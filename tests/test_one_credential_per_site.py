"""Only the latest login / API key / 2FA key is kept for a site. Written, not run (test pause).

Why: with several saved logins for one site the agent was asked to choose between them and tried the wrong one ("Invalid credentials")."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from chainlit.auth import get_current_user
from fastapi import FastAPI, Request
from sqlalchemy import select

import app.core.runtime as rt
from app.agent.access_planner import api_credentials, usable_credentials
from app.db.models import Credential
from app.vault.kms import LocalKms
from app.vault.router import router
from app.vault.service import CredentialView, VaultService

SEED = "JBSWY3DPEHPK3PXP"
KEY1, KEY2 = "sk-or-v1-" + "a1" * 20, "sk-or-v1-" + "b2" * 20


@pytest.fixture
async def vault(svc):
    return VaultService(svc.db, LocalKms())


@pytest.fixture
async def alice(svc):
    return (await svc.tenancy.ensure_user(google_sub="g-a", email="a@x.com", name="A")).id


@pytest.fixture
async def bob(svc):
    return (await svc.tenancy.ensure_user(google_sub="g-b", email="b@x.com", name="B")).id


def pw(vault, user, site="one9founders.com", username="me@x.com", **kw):
    return vault.store(user_id=user, kind="password", label=f"{site} login", secret={"username": username, "password": "pw-" + username},
                       origins=[site], **kw)


async def active(vault, user, kind=None):
    return [c for c in await vault.list(user) if kind in (None, c.kind)]


async def test_a_newer_login_for_the_same_site_replaces_the_older(vault, alice):
    old = await pw(vault, alice, username="old@x.com")
    new = await pw(vault, alice, username="new@x.com")
    assert [c.id for c in await active(vault, alice, "password")] == [new.id] and new.replaced == (old.id,)


async def test_three_saves_leave_one(vault, alice):
    for u in ("a", "b", "c"):
        last = await pw(vault, alice, username=f"{u}@x.com")
    assert [c.id for c in await active(vault, alice)] == [last.id]


async def test_the_old_one_is_really_gone_its_ciphertext_deleted_and_it_cannot_be_used(vault, svc, alice):
    old = await pw(vault, alice, username="old@x.com")
    await pw(vault, alice, username="new@x.com")
    async with svc.db.session() as s:
        row = await s.get(Credential, old.id)
        assert row.revoked_at is not None and row.ciphertext == b"" and row.nonce == b""
    with pytest.raises(Exception):
        await vault.use(user_id=alice, credential_id=old.id, origin="https://one9founders.com", actor="agent")


async def test_other_sites_and_other_users_are_untouched(vault, alice, bob):
    other_site = await pw(vault, alice, site="swiggy.com")
    bobs = await pw(vault, bob)
    await pw(vault, alice)
    assert {c.id for c in await active(vault, alice)} >= {other_site.id}
    assert [c.id for c in await active(vault, bob)] == [bobs.id]


async def test_the_old_logins_2fa_key_goes_with_it_and_the_new_ones_stays(vault, alice):
    old = await pw(vault, alice, username="old@x.com")
    old_2fa = await vault.store(user_id=alice, kind="totp", label="old 2FA", secret={"seed": SEED}, origins=["one9founders.com"],
                                linked_credential_id=old.id)
    new = await pw(vault, alice, username="new@x.com")
    assert old_2fa.id in new.replaced
    new_2fa = await vault.store(user_id=alice, kind="totp", label="new 2FA", secret={"seed": SEED}, origins=["one9founders.com"],
                                linked_credential_id=new.id)
    assert {c.id for c in await active(vault, alice)} == {new.id, new_2fa.id}        # saving the 2FA key did not retire its own login


async def test_a_credential_that_also_covers_other_sites_is_left_alone(vault, alice):
    wide = await vault.store(user_id=alice, kind="password", label="two sites", secret={"username": "u", "password": "p"},
                             origins=["one9founders.com", "example.org"])
    new = await pw(vault, alice)
    assert new.replaced == () and {c.id for c in await active(vault, alice)} == {wide.id, new.id}


async def test_a_login_with_only_a_subset_of_sites_is_replaced_by_one_covering_them(vault, alice):
    small = await pw(vault, alice)
    big = await vault.store(user_id=alice, kind="password", label="both", secret={"username": "u", "password": "p"},
                            origins=["one9founders.com", "example.org"])
    assert big.replaced == (small.id,)


async def test_api_keys_for_the_same_site_are_replaced(vault, alice):
    a = await vault.store(user_id=alice, kind="api_key", label="OR 1", secret={"key": KEY1}, origins=["openrouter.ai"])
    b = await vault.store(user_id=alice, kind="api_key", label="OR 2", secret={"key": KEY2}, origins=["openrouter.ai"])
    assert b.replaced == (a.id,) and [c.id for c in await active(vault, alice, "api_key")] == [b.id]


async def test_api_keys_with_no_site_are_never_guessed_to_be_duplicates(vault, alice):
    a = await vault.store(user_id=alice, kind="api_key", label="k1", secret={"key": KEY1})
    b = await vault.store(user_id=alice, kind="api_key", label="k2", secret={"key": KEY2})
    assert b.replaced == () and {c.id for c in await active(vault, alice)} == {a.id, b.id}


async def test_cards_are_not_replaced_a_site_can_have_several(vault, alice):
    for n in ("4111111111111111", "5500000000000004"):
        await vault.store(user_id=alice, kind="card", label="card", origins=["one9founders.com"], secret={"number": n, "cvv": "123", "exp": "12/30"})
    assert len(await active(vault, alice, "card")) == 2


async def test_each_replacement_is_in_the_access_log_with_the_reason(vault, alice):
    old = await pw(vault, alice, username="old@x.com")
    await pw(vault, alice, username="new@x.com")
    rows = [r for r in await vault.access_log(alice, old.id) if r.action == "revoke"]
    assert rows and rows[0].actor == "system" and "newer" in (rows[0].reason or "")


# ───────────── the agent is never asked to choose between logins ─────────────

def view(id_, kind, when, origins=("one9founders.com",), **meta):
    return CredentialView(id_, kind, id_, list(origins), meta, when, None, False)


def test_duplicates_saved_before_this_rule_still_yield_one_login_the_newest():
    t = datetime(2026, 10, 1, tzinfo=timezone.utc)
    creds = [view("cred_old", "password", t), view("cred_new", "password", t + timedelta(days=2)), view("cred_mid", "password", t + timedelta(days=1)),
             view("t_old", "totp", t), view("t_new", "totp", t + timedelta(days=2))]
    rules = SimpleNamespace(policies={})
    got = usable_credentials("one9founders.com", creds, rules)
    assert sorted(c.credential_id for c in got) == ["cred_new", "t_new"]


def test_the_newest_api_key_wins_when_old_duplicates_exist():
    t = datetime(2026, 10, 1, tzinfo=timezone.utc)
    creds = [view("k_old", "api_key", t, ("openrouter.ai",), last4="1111"), view("k_new", "api_key", t + timedelta(days=1), ("openrouter.ai",), last4="2222")]
    got = api_credentials("openrouter.ai", creds, SimpleNamespace(policies={}))
    assert [c.credential_id for c in got] == ["k_new"]


def test_another_sites_login_is_never_offered():
    t = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert usable_credentials("one9founders.com", [view("x", "password", t, ("example.org",))], SimpleNamespace(policies={})) == []


# ───────────── over HTTP: the browser's saved session for a replaced login is cleared ─────────────

@pytest.fixture
async def web(svc, vault, monkeypatch):
    cleared = []

    class Sessions:
        async def revoke(self, uid, site, cdp_url=None):
            cleared.append((uid, site))
            return True
    monkeypatch.setattr(rt, "_runtime", SimpleNamespace(vault=vault, sessions=Sessions()))
    app = FastAPI()
    app.include_router(router)

    async def who(request: Request):
        uid = request.headers.get("x-test-user")
        return SimpleNamespace(metadata={"user_id": uid}) if uid else None
    app.dependency_overrides[get_current_user] = who
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield SimpleNamespace(c=c, cleared=cleared)


async def save(web, uid, username):
    return await web.c.post("/chainlit/vault/credentials", headers={"X-Requested-With": "nami", "x-test-user": uid},
                            json={"kind": "password", "label": "L", "origins": ["one9founders.com"], "secret": {"username": username, "password": "pw"}})


async def test_replacing_a_login_clears_the_old_browser_session_for_that_site(web, alice):
    first = await save(web, alice, "old@x.com")
    assert "replaced" not in first.json() and web.cleared == []                      # nothing to replace, nothing to clear
    second = await save(web, alice, "new@x.com")
    await asyncio.sleep(0.05)
    assert second.json()["replaced"] == 1 and web.cleared == [(alice, "one9founders.com")]
    assert "pw" not in second.text and "new@x.com" in second.text                      # still no secret in the reply


async def test_a_first_save_clears_no_session(web, alice):
    await save(web, alice, "me@x.com")
    await asyncio.sleep(0.05)
    assert web.cleared == []
