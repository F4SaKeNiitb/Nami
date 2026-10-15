"""Disconnect Google: the local copy is wiped first, then Google is asked to cancel the grant (best effort, token never logged)."""

import logging
from types import SimpleNamespace

import httpx
import pytest
from chainlit.auth import get_current_user
from fastapi import FastAPI, Request

import app.core.runtime as rt
from app.browser.session_manager import SessionManager
from app.connect import google_revoke
from app.connect.google_oauth import REVOKE_URL, GoogleOAuth
from app.vault.kms import LocalKms
from app.vault.router import router
from app.vault.service import VaultService

REFRESH = "1//0g-REFRESH-TOKEN-" + "x" * 40


def make_oauth(handler):
    return GoogleOAuth(client_id="cid", client_secret="s", signing_key="k",
                       client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.fixture
async def world(svc, monkeypatch):
    vault = VaultService(svc.db, LocalKms())
    uid = (await svc.tenancy.ensure_user(google_sub="g-r", email="r@x.com", name="R")).id
    calls = []

    def handler(request: httpx.Request):
        calls.append(request)
        return httpx.Response(200)
    sessions = SessionManager(svc.db, vault)
    dropped = []

    async def revoke_session(user_id, site, cdp_url=None):
        dropped.append((user_id, site))
    monkeypatch.setattr(sessions, "revoke", revoke_session)
    runtime = SimpleNamespace(vault=vault, sessions=sessions, orchestrator=SimpleNamespace(google_oauth=make_oauth(handler)))
    monkeypatch.setattr(rt, "_runtime", runtime)
    app = FastAPI()
    app.include_router(router)

    async def who(request: Request):
        return SimpleNamespace(metadata={"user_id": request.headers["x-test-user"]})
    app.dependency_overrides[get_current_user] = who
    google = await vault.store(user_id=uid, kind="oauth", label="Google (me)", origins=["google.com", "googleapis.com"],
                               secret={"refresh_token": REFRESH, "scopes": "x", "account": "me", "provider": "google"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield SimpleNamespace(c=c, uid=uid, vault=vault, calls=calls, dropped=dropped, google=google, runtime=runtime)


def hdr(uid):
    return {"X-Requested-With": "nami", "x-test-user": uid}


async def test_disconnect_revokes_at_google_and_forgets_the_saved_session(world):
    r = await world.c.delete(f"/chainlit/vault/credentials/{world.google.id}", headers=hdr(world.uid))
    assert r.status_code == 200 and r.json()["google"] == {"grant": True, "session": True}
    assert [str(c.url) for c in world.calls] == [REVOKE_URL] and b"token=" in world.calls[0].content
    assert world.dropped == [(world.uid, "google.com")]
    assert REFRESH not in r.text                                    # never echoed back
    assert await world.vault.list(world.uid) == []                  # the local copy is gone


async def test_a_failing_google_never_blocks_the_local_delete_or_leaks_the_token(world, caplog):
    caplog.set_level(logging.DEBUG)

    def boom(request):
        raise httpx.ConnectError("down", request=request)
    world.runtime.orchestrator.google_oauth = make_oauth(boom)
    r = await world.c.delete(f"/chainlit/vault/credentials/{world.google.id}", headers=hdr(world.uid))
    assert r.status_code == 200 and r.json()["revoked"] is True and r.json()["google"]["grant"] is False
    assert await world.vault.list(world.uid) == []
    assert REFRESH not in caplog.text


async def test_a_slow_google_is_cut_off(world, monkeypatch):
    import asyncio
    monkeypatch.setattr(google_revoke, "REVOKE_TIMEOUT", 0.05)

    async def slow(token):
        await asyncio.sleep(5)
    world.runtime.orchestrator.google_oauth = SimpleNamespace(revoke=slow)
    out = await google_revoke.revoke_at_google(world.runtime, world.uid, REFRESH)
    assert out["grant"] is False and out["session"] is True


async def test_other_credentials_do_not_call_google(world):
    pw = await world.vault.store(user_id=world.uid, kind="password", label="s", origins=["swiggy.com"],
                                 secret={"username": "u", "password": "pw-unique-123"})
    r = await world.c.delete(f"/chainlit/vault/credentials/{pw.id}", headers=hdr(world.uid))
    assert r.json()["revoked"] is True and "google" not in r.json() and world.calls == []      # Google is never called for another site's login
    assert world.dropped == [(world.uid, "swiggy.com")]                  # but that site's own session is signed out


async def test_another_users_google_grant_cannot_be_revoked(world, svc):
    other = (await svc.tenancy.ensure_user(google_sub="g-o", email="o@x.com", name="O")).id
    r = await world.c.delete(f"/chainlit/vault/credentials/{world.google.id}", headers=hdr(other))
    assert r.status_code == 404 and world.calls == []
