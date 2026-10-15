"""How an API key is sent: a closed set of shapes, validated; the model only proposes among them; the host never changes."""

import base64
import http.server
import json
import logging
import threading
from unittest import mock

import pytest

from app.agent.schemas import AccessPlan, CredentialUse
from app.browser import api_auth
from app.browser.api_auth import DEFAULTS, Scheme, SchemeError, parse_scheme, propose, try_parse
from app.browser.api_executor import ApiError, ApiExecutor
from app.browser.broker import Broker
from app.sites.loader import SiteHints
from app.vault.kms import LocalKms
from app.vault.service import VaultError, VaultService

KEY = "zz-live-0123456789abcdef0123456789abcdef"


# ───────────── the shapes ─────────────

def test_each_shape_puts_the_key_where_it_says():
    assert Scheme("bearer").apply(KEY)[0]["Authorization"] == f"Bearer {KEY}"
    assert Scheme("header", name="x-api-key").apply(KEY)[0]["x-api-key"] == KEY
    assert Scheme("header", name="Authorization", prefix="Token ").apply(KEY)[0]["Authorization"] == f"Token {KEY}"
    assert Scheme("basic").apply(KEY)[0]["Authorization"] == "Basic " + base64.b64encode(f"{KEY}:".encode()).decode()
    h, q = Scheme("query", name="key").apply(KEY)
    assert q == {"key": KEY} and KEY not in json.dumps(h)


def test_a_fixed_header_rides_along_and_is_not_secret():
    s = parse_scheme({"kind": "header", "name": "x-api-key", "extra_headers": {"anthropic-version": "2023-06-01"}})
    h, _ = s.apply(KEY)
    assert h["anthropic-version"] == "2023-06-01" and h["x-api-key"] == KEY
    assert "anthropic-version" in s.describe() and KEY not in s.describe()


@pytest.mark.parametrize("bad", [
    {"kind": "cookie", "name": "sid"},
    {"kind": "header", "name": "Host"}, {"kind": "header", "name": "Cookie"}, {"kind": "header", "name": "X-Forwarded-For"},
    {"kind": "header", "name": "bad name"}, {"kind": "header", "name": "x\r\nHost: evil"}, {"kind": "header", "name": ""},
    {"kind": "query", "name": "a=b&c"}, {"kind": "query", "name": ""},
    {"kind": "bearer", "name": "x-api-key"},
    {"kind": "header", "name": "x-api-key", "prefix": "Tok3n!"},
    {"kind": "query", "name": "key", "prefix": "Token "},
    {"kind": "bearer", "extra_headers": {"Host": "evil.example"}},
    {"kind": "bearer", "extra_headers": {"Authorization": "Bearer other"}},
    {"kind": "bearer", "extra_headers": {"Cookie": "a=b"}},
    {"kind": "bearer", "extra_headers": {"x-a": "line\r\nbreak"}},
    {"kind": "bearer", "extra_headers": {"x-a": "1", "x-b": "2", "x-c": "3", "x-d": "4"}},
    "unknown", None, 5,
])
def test_anything_outside_the_allowed_shapes_is_refused(bad):
    with pytest.raises(SchemeError):
        parse_scheme(bad)
    assert try_parse(bad) is None


def test_a_scheme_survives_being_saved_and_read_back():
    s = parse_scheme({"kind": "header", "name": "Authorization", "prefix": "Token", "extra_headers": {"x-v": "1"}})
    assert parse_scheme(s.to_dict()) == s


def test_registry_values():
    assert api_auth.from_registry({"auth": "bearer"}) == Scheme("bearer")
    assert api_auth.from_registry({"base": "https://a.example"}) == Scheme("bearer")                      # no value means bearer
    assert api_auth.from_registry({"auth": "auto"}) is None and api_auth.is_auto({"auth": "auto"})
    assert api_auth.from_registry({"auth": {"kind": "query", "name": "key"}}) == Scheme("query", name="key")
    assert api_auth.from_registry(None) is None


# ───────────── asking the model ─────────────

class FakeLLM:
    def __init__(self, reply=None, boom=False):
        self.reply, self.boom, self.user = reply, boom, ""

    async def json(self, *, system, user, schema, **kw):
        self.user = user
        if self.boom:
            raise RuntimeError("model down")
        return schema.model_validate(self.reply)


async def test_the_model_sees_the_site_and_endpoints_never_a_key():
    llm = FakeLLM({"schemes": [{"kind": "header", "name": "x-api-key"}]})
    out = await propose(llm, site="example.com", host="api.example.com", endpoints={"me": {"summary": "your profile"}})
    assert out[0] == Scheme("header", name="x-api-key")
    assert "example.com" in llm.user and "your profile" in llm.user and KEY not in llm.user


async def test_proposals_are_validated_deduplicated_and_topped_up_with_defaults():
    llm = FakeLLM({"schemes": [{"kind": "header", "name": "Host"},                      # forbidden: dropped
                               {"kind": "header", "name": "x-token"}, {"kind": "header", "name": "x-token"}]})
    out = await propose(llm, site="a.com", host="api.a.com", endpoints={})
    assert Scheme("header", name="Host") not in out
    assert out[0] == Scheme("header", name="x-token") and out.count(out[0]) == 1
    assert len(out) <= api_auth.MAX_TRIES and Scheme("bearer") in out


async def test_a_model_failure_falls_back_to_the_common_defaults():
    out = await propose(FakeLLM(boom=True), site="a.com", host="api.a.com", endpoints={})
    assert out == list(DEFAULTS)[:api_auth.MAX_TRIES]


async def test_the_model_cannot_name_a_host_or_url():
    assert not any(f in api_auth.SchemeGuess.model_fields for f in ("host", "url", "base", "path"))
    llm = FakeLLM({"schemes": [{"kind": "bearer", "extra_headers": {"Location": "http://evil.example/steal"}}]})
    out = await propose(llm, site="a.com", host="api.a.com", endpoints={})
    assert all("evil.example" not in v for s in out for _, v in s.extra)         # a URL is not a valid fixed-header value


# ───────────── trying a key on a server that only accepts one way ─────────────

class Fake(http.server.BaseHTTPRequestHandler):
    mode = "x-api-key"                     # which way this server takes the key
    seen: list[dict] = []
    status_other = 401
    other_hits: list[str] = []

    def log_message(self, *a): ...

    def do_GET(self):
        h = self.headers
        got = {"path": self.path, "authz": h.get("Authorization"), "x-api-key": h.get("x-api-key"), "ver": h.get("anthropic-version")}
        Fake.seen.append(got)
        ok = {"bearer": h.get("Authorization") == f"Bearer {KEY}", "x-api-key": h.get("x-api-key") == KEY,
              "basic": h.get("Authorization") == "Basic " + base64.b64encode(f"{KEY}:".encode()).decode(),
              "query": f"key={KEY}" in self.path,
              "versioned": h.get("x-api-key") == KEY and h.get("anthropic-version") == "2023-06-01"}[Fake.mode]
        if self.path.startswith("/api/redirect"):
            self.send_response(302); self.send_header("Location", f"http://localhost:{Fake.other_port}/steal"); self.end_headers(); return
        code = 200 if ok else Fake.status_other
        body = json.dumps({"data": [{"id": "m1"}], "echo": KEY if ok else ""}).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)


class Other(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): ...
    def do_GET(self):
        Fake.other_hits.append("hit"); self.send_response(200); self.end_headers()


@pytest.fixture
def server():
    a = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    o = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Other)
    Fake.other_port = o.server_address[1]
    Fake.seen.clear(); Fake.other_hits.clear(); Fake.mode, Fake.status_other = "x-api-key", 401
    for s in (a, o):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield a.server_address[1]
    a.shutdown(); o.shutdown()


def hints(port, auth="auto", path="/me"):
    return SiteHints(site="127.0.0.1", api={"base": f"http://127.0.0.1:{port}/api", "auth": auth,
                                           "endpoints": {"me": {"path": path, "summary": "who am i"}}})


@pytest.fixture
async def world(svc, server):
    vault = VaultService(svc.db, LocalKms())
    uid = (await svc.tenancy.ensure_user(google_sub="g", email="a@x.com", name="A")).id
    cred = await vault.store(user_id=uid, kind="api_key", label="K", origins=["127.0.0.1"], secret={"key": KEY})
    access = AccessPlan(allowed_domains=["127.0.0.1"], login="api",
                        credentials=[CredentialUse(credential_id=cred.id, kind="api_key", label="K", site="127.0.0.1")])
    broker, ex = Broker(vault, user_id=uid, task_id="t1"), ApiExecutor()
    state = {"auth": "auto", "path": "/me"}
    with mock.patch("app.browser.api_executor.hints_for", lambda s: hints(server, state["auth"], state["path"])):
        yield type("W", (), dict(vault=vault, uid=uid, cred=cred, access=access, broker=broker, ex=ex, state=state, svc=svc))


async def _stored(w):
    return next(c for c in await w.vault.list(w.uid) if c.id == w.cred.id).meta.get("auth_scheme")


async def test_an_unknown_way_is_detected_before_any_call(world):
    assert world.ex.needs_discovery(world.access, "127.0.0.1")
    with pytest.raises(ApiError, match="worked out"):
        await world.ex.call(broker=world.broker, access=world.access, site="127.0.0.1", endpoint="me")
    assert Fake.seen == []                                           # nothing was sent, and the key was not decrypted


async def test_discovery_does_nothing_without_approval(world):
    with pytest.raises(ApiError, match="approval"):
        await world.ex.discover(broker=world.broker, access=world.access, site="127.0.0.1", candidates=list(DEFAULTS), approved=False)
    assert Fake.seen == [] and not [r for r in await world.vault.access_log(world.uid) if r.action in ("inject", "request")]


@pytest.mark.parametrize("mode,expect", [("bearer", Scheme("bearer")), ("x-api-key", Scheme("header", name="x-api-key")),
                                         ("basic", Scheme("basic")), ("query", Scheme("query", name="key"))])
async def test_discovery_finds_the_way_the_server_takes_the_key_and_remembers_it(world, mode, expect):
    Fake.mode = mode
    tries = [Scheme("bearer"), Scheme("header", name="x-api-key"), Scheme("basic"), Scheme("query", name="key")]
    got = await world.ex.discover(broker=world.broker, access=world.access, site="127.0.0.1", candidates=tries, approved=True)
    assert got == expect
    assert parse_scheme(await _stored(world)) == expect            # saved on the credential, so the next task skips this
    assert len(Fake.seen) == tries.index(expect) + 1               # stopped at the first that worked


async def test_a_remembered_scheme_is_used_next_time_without_discovery(world):
    Fake.mode = "x-api-key"
    await world.ex.discover(broker=world.broker, access=world.access, site="127.0.0.1",
                            candidates=[Scheme("bearer"), Scheme("header", name="x-api-key")], approved=True)
    fresh = [c for c in await world.vault.list(world.uid)][0]
    access = AccessPlan(allowed_domains=["127.0.0.1"], login="api", credentials=[
        CredentialUse(credential_id=fresh.id, kind="api_key", label="K", site="127.0.0.1", auth_scheme=fresh.meta.get("auth_scheme"))])
    assert not world.ex.needs_discovery(access, "127.0.0.1")
    Fake.seen.clear()
    r = await world.ex.call(broker=world.broker, access=access, site="127.0.0.1", endpoint="me")
    assert r.status == 200 and len(Fake.seen) == 1 and Fake.seen[0]["x-api-key"] == KEY
    assert KEY not in r.text                                       # the server echoed it; it was redacted


async def test_a_version_header_goes_with_the_key(world):
    Fake.mode = "versioned"
    s = parse_scheme({"kind": "header", "name": "x-api-key", "extra_headers": {"anthropic-version": "2023-06-01"}})
    assert await world.ex.discover(broker=world.broker, access=world.access, site="127.0.0.1", candidates=[Scheme("bearer"), s], approved=True) == s


async def test_when_every_way_is_refused_it_says_so_and_nothing_is_saved(world):
    Fake.mode = "basic"
    with pytest.raises(ApiError, match="refused your key every way"):
        await world.ex.discover(broker=world.broker, access=world.access, site="127.0.0.1",
                                candidates=[Scheme("bearer"), Scheme("header", name="x-api-key")], approved=True)
    assert await _stored(world) is None and len(Fake.seen) == 2


async def test_tries_are_capped(world):
    Fake.mode = "basic"
    many = [Scheme("header", name=f"x-k{i}") for i in range(10)]
    with pytest.raises(ApiError):
        await world.ex.discover(broker=world.broker, access=world.access, site="127.0.0.1", candidates=many, approved=True)
    assert len(Fake.seen) == api_auth.MAX_TRIES


async def test_an_unclear_answer_stops_the_tries_instead_of_spraying_the_key(world):
    Fake.status_other = 404                                        # not 401/403: we can't tell what is wrong
    with pytest.raises(ApiError, match="couldn't tell"):
        await world.ex.discover(broker=world.broker, access=world.access, site="127.0.0.1", candidates=list(DEFAULTS), approved=True)
    assert len(Fake.seen) == 1


async def test_a_redirect_is_never_followed_during_discovery(world):
    world.state["path"] = "/redirect"
    with pytest.raises(ApiError):
        await world.ex.discover(broker=world.broker, access=world.access, site="127.0.0.1", candidates=list(DEFAULTS), approved=True)
    assert Fake.other_hits == [] and len(Fake.seen) == 1


async def test_the_key_only_ever_goes_to_the_registry_host_whatever_the_scheme(world):
    Fake.mode = "query"
    await world.ex.discover(broker=world.broker, access=world.access, site="127.0.0.1", candidates=[Scheme("query", name="key")], approved=True)
    assert Fake.other_hits == []


async def test_a_key_in_the_url_is_not_logged_or_audited(world, caplog):
    Fake.mode = "query"
    with caplog.at_level(logging.DEBUG):
        await world.ex.discover(broker=world.broker, access=world.access, site="127.0.0.1", candidates=[Scheme("query", name="key")], approved=True)
    assert KEY not in caplog.text
    rows = [r for r in await world.vault.access_log(world.uid) if r.action == "request"]
    assert rows and all(KEY not in (r.reason or "") and KEY not in (r.origin or "") for r in rows)


async def test_a_learned_scheme_is_forgotten_when_the_site_then_says_401(world):
    Fake.mode = "x-api-key"
    await world.ex.discover(broker=world.broker, access=world.access, site="127.0.0.1", candidates=[Scheme("header", name="x-api-key")], approved=True)
    assert await _stored(world)
    Fake.mode = "bearer"                                           # the site changed how it takes keys
    stale = AccessPlan(allowed_domains=["127.0.0.1"], login="api", credentials=[
        CredentialUse(credential_id=world.cred.id, kind="api_key", label="K", site="127.0.0.1", auth_scheme={"kind": "header", "name": "x-api-key"})])
    with pytest.raises(ApiError, match="rejected"):
        await world.ex.call(broker=world.broker, access=stale, site="127.0.0.1", endpoint="me")
    assert await _stored(world) is None                            # so the next task works it out again


async def test_a_declared_scheme_is_not_forgotten_on_a_401(world):
    world.state["auth"] = "bearer"
    Fake.mode = "x-api-key"
    with pytest.raises(ApiError, match="rejected"):
        await world.ex.call(broker=world.broker, access=world.access, site="127.0.0.1", endpoint="me")


async def test_a_declared_scheme_needs_no_discovery(world):
    world.state["auth"] = {"kind": "header", "name": "x-api-key"}
    Fake.mode = "x-api-key"
    assert not world.ex.needs_discovery(world.access, "127.0.0.1")
    r = await world.ex.call(broker=world.broker, access=world.access, site="127.0.0.1", endpoint="me")
    assert r.status == 200 and Fake.seen[0]["x-api-key"] == KEY and Fake.seen[0]["authz"] is None


# ───────────── the vault side ─────────────

async def test_only_allowed_shapes_can_be_saved_on_a_credential(world):
    for bad in ({"kind": "header", "name": "Host"}, {"kind": "nope"}, "x"):
        with pytest.raises(VaultError):
            await world.vault.set_auth_scheme(user_id=world.uid, credential_id=world.cred.id, scheme=bad)
    assert await _stored(world) is None


async def test_another_user_cannot_set_the_scheme_and_a_password_has_none(world):
    other = (await world.svc.tenancy.ensure_user(google_sub="g2", email="b@x.com", name="B")).id
    with pytest.raises(Exception):
        await world.vault.set_auth_scheme(user_id=other, credential_id=world.cred.id, scheme={"kind": "bearer"})
    pw = await world.vault.store(user_id=world.uid, kind="password", label="p", origins=["127.0.0.1"], secret={"username": "u", "password": "pw-pw-pw"})
    with pytest.raises(Exception):
        await world.vault.set_auth_scheme(user_id=world.uid, credential_id=pw.id, scheme={"kind": "bearer"})


async def test_the_saved_scheme_holds_no_secret_and_the_change_is_logged(world):
    await world.vault.set_auth_scheme(user_id=world.uid, credential_id=world.cred.id, scheme={"kind": "header", "name": "x-api-key"})
    assert KEY not in json.dumps(await _stored(world))
    assert any(r.action == "scheme" for r in await world.vault.access_log(world.uid))
    await world.vault.set_auth_scheme(user_id=world.uid, credential_id=world.cred.id, scheme=None)
    assert await _stored(world) is None
