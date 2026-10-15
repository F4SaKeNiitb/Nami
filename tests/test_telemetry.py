"""Telemetry: spans carry identifiers and verdicts, never page text, screenshots, prompts or secrets."""

import pytest
from opentelemetry.sdk.trace.export import SpanExportResult

from app.security.authz import AuthzService, Principal, Resource
from app.telemetry import Telemetry, sanitize_attrs

KEY = "sk-or-v1-abcdef0123456789abcdef0123456789"


class Collect:
    def __init__(self): self.spans = []
    def export(self, spans): self.spans += list(spans); return SpanExportResult.SUCCESS
    def shutdown(self): ...
    def force_flush(self, t=0): return True


@pytest.fixture
def tel():
    t, c = Telemetry(), Collect()
    t.configure(c, salt="s")
    t.out = c
    yield t
    t.shutdown()


def test_forbidden_attribute_names_are_dropped_not_masked():
    out = sanitize_attrs({"page_text": "Total 486", "screenshot": b"\x89PNG", "prompt": "hi", "password": "x", "llm.model": "m",
                          "llm.prompt_chars": 120, "dom": "<html>", "cookie_header": "a=b", "api_key": KEY, "seed_len": 16})
    assert out == {"llm.model": "m", "llm.prompt_chars": 120, "seed_len": 16}


def test_secret_shaped_values_and_bytes_are_scrubbed_in_allowed_attributes():
    out = sanitize_attrs({"note": f"key {KEY} used", "blob": b"x", "items": [f"{KEY}", b"y", "ok"], "long": "a" * 1000})
    assert KEY not in str(out) and "blob" not in out and "b'y'" not in str(out) and len(out["long"]) == 200


def test_exported_spans_are_redacted_even_when_created_with_raw_attributes(tel):
    with tel._tracer.start_as_current_span("third.party") as sp:          # bypasses our helper entirely
        sp.set_attribute("page_text", "Order total 486 and key " + KEY)
        sp.set_attribute("screenshot_b64", "AAAA")
        sp.set_attribute("http.url", f"https://x.com/?k={KEY}")
        sp.add_event("log", {"completion": "secret words", "n": 3})
    s = tel.out.spans[0]
    flat = str(dict(s.attributes)) + str([dict(e.attributes) for e in s.events])
    assert KEY not in flat and "page_text" not in flat and "screenshot" not in flat and "completion" not in flat
    assert s.events[0].attributes == {"n": 3}


def test_error_status_description_is_redacted(tel):
    with pytest.raises(RuntimeError):
        with tel.span("x"):
            raise RuntimeError(f"failed with {KEY}")
    st = tel.out.spans[0].status
    assert KEY not in (st.description or "") and st.status_code.name == "ERROR"


def test_spans_carry_org_task_and_a_salted_user_hash_not_the_user_id(tel):
    with tel.span("graph.run", org_id="o1", user_id="user-123", task_id="t1"):
        pass
    a = dict(tel.out.spans[0].attributes)
    assert a["nami.org_id"] == "o1" and a["nami.task_id"] == "t1" and "user-123" not in str(a)
    assert a["nami.user_hash"] == tel.user_hash("user-123") and a["nami.user_hash"] != tel.user_hash("user-124")
    other = Telemetry(); other.configure(Collect(), salt="different")
    assert other.user_hash("user-123") != tel.user_hash("user-123")           # hashes are not joinable across deployments


def test_disabled_telemetry_is_a_noop():
    t = Telemetry()
    with t.span("x", page_text="a") as sp:
        assert sp is None
    t.inc("a")
    assert t.count("a") == 0


async def test_denied_authorisation_emits_a_span_and_a_metric(svc, tel, monkeypatch):
    import app.security.authz as az
    monkeypatch.setattr(az, "tel", tel)
    p = Principal(user_id="u1", org_id="o1", role="viewer")
    d = await svc.authz.check(p, "start_task", Resource("org", "o1", id="o1"))
    assert not d.allow
    span = [s for s in tel.out.spans if s.name == "authz.check"][0]
    assert dict(span.attributes)["authz.decision"] == "deny" and dict(span.attributes)["nami.org_id"] == "o1"
    assert tel.count("authz_denied_total", action="start_task") == 1


async def test_vault_use_span_has_verdict_but_no_secret(svc, tel, monkeypatch):
    import app.vault.service as vs
    from app.vault.kms import LocalKms
    monkeypatch.setattr(vs, "tel", tel)
    v = vs.VaultService(svc.db, LocalKms())
    uid = (await svc.tenancy.ensure_user(google_sub="g", email="a@x.com", name="A")).id
    c = await v.store(user_id=uid, kind="password", label="x", origins=["acme.com"], secret={"username": "me", "password": "Tr0ub4dor&3-unique"})
    await v.use(user_id=uid, credential_id=c.id, origin="https://acme.com", actor="t", task_id="t1")
    with pytest.raises(Exception):
        await v.use(user_id=uid, credential_id=c.id, origin="https://evil.com", actor="t", task_id="t1")
    spans = [dict(s.attributes) for s in tel.out.spans if s.name == "vault.use"]
    assert [a["vault.result"] for a in spans] == ["allowed", "denied"]
    assert "Tr0ub4dor" not in str(spans) and "me@" not in str(spans)
    assert tel.count("secret_injections_total", result="denied") == 1


def test_token_usage_is_attributed_per_task_even_with_tracing_off():
    from app.telemetry import current_task
    t = Telemetry()                                           # not configured: tracing off
    t.add_usage(5, 5)                                         # no current task: ignored
    tok = current_task.set("t1")
    t.add_usage(100, 20); t.add_usage(50, 10)
    current_task.set("t2"); t.add_usage(1, 1)
    current_task.reset(tok)
    assert t.pop_usage("t1") == {"input_tokens": 150, "output_tokens": 30, "calls": 2}
    assert t.pop_usage("t1") == {"input_tokens": 0, "output_tokens": 0, "calls": 0}      # popped once
    assert t.pop_usage("t2")["calls"] == 1
