"""Revoking a login must really sign the user out: the saved session AND the cookies in the browser profile that keeps them signed in."""

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from chainlit.auth import get_current_user

import app.core.runtime as rt
from app.browser import session_manager as sm
from app.browser.provider import ProfileBusy
from app.browser.session_manager import SessionManager
from app.browser.snapshots import SiteSnapshot
from app.vault.kms import LocalKms
from app.vault.router import router
from app.vault.service import VaultService

H = {"X-Requested-With": "nami"}


class FakeProvider:
    def __init__(self, busy=False):
        self.busy, self.log = busy, []

    async def open(self, user_id, *, headless=True):
        if self.busy:
            raise ProfileBusy("in use")
        self.log.append(("open", user_id, headless))
        return SimpleNamespace(cdp_url="ws://profile", user_id=user_id)

    async def close(self, session):
        self.log.append(("close", session.user_id))


@pytest.fixture
async def world(svc, monkeypatch):
    vault = VaultService(svc.db, LocalKms())
    cleared = []

    async def clear_site(url, site):
        cleared.append((url, site))

    async def snapshot_site(url, site):
        return SiteSnapshot(site=site, cookies=[{"name": "sid", "value": "v", "domain": site}], local_storage={})
    monkeypatch.setattr(sm, "clear_site", clear_site)
    monkeypatch.setattr(sm, "snapshot_site", snapshot_site)
    uid = (await svc.tenancy.ensure_user(google_sub="g", email="a@x.com", name="A")).id
    prov = FakeProvider()
    mgr = SessionManager(svc.db, vault, provider=prov)
    return SimpleNamespace(svc=svc, vault=vault, cleared=cleared, uid=uid, prov=prov, mgr=mgr, monkeypatch=monkeypatch)


async def test_revoke_with_no_browser_open_clears_the_profile_through_a_short_headless_one(world):
    await world.mgr.capture(world.uid, "ws://x", "swiggy.com")
    assert await world.mgr.revoke(world.uid, "swiggy.com") is True
    assert world.cleared == [("ws://profile", "swiggy.com")]
    assert world.prov.log == [("open", world.uid, True), ("close", world.uid)]      # headless, and always closed again
    assert await world.mgr.sites(world.uid) == set()


async def test_revoke_in_a_live_browser_uses_it_and_opens_nothing(world):
    assert await world.mgr.revoke(world.uid, "swiggy.com", cdp_url="ws://live") is True
    assert world.cleared == [("ws://live", "swiggy.com")] and world.prov.log == []


async def test_a_busy_profile_still_drops_the_snapshot_and_says_the_browser_was_not_cleared(world):
    await world.mgr.capture(world.uid, "ws://x", "swiggy.com")
    world.prov.busy = True
    assert await world.mgr.revoke(world.uid, "swiggy.com") is False
    assert world.cleared == [] and await world.mgr.sites(world.uid) == set()


async def test_a_failure_while_clearing_closes_the_browser_and_still_drops_the_snapshot(world):
    async def boom(url, site):
        raise RuntimeError("cdp dropped")
    world.monkeypatch.setattr(sm, "clear_site", boom)
    await world.mgr.capture(world.uid, "ws://x", "swiggy.com")
    assert await world.mgr.revoke(world.uid, "swiggy.com") is False
    assert world.prov.log[-1] == ("close", world.uid) and await world.mgr.sites(world.uid) == set()


async def test_without_a_provider_the_old_behaviour_holds(world):
    mgr = SessionManager(world.svc.db, world.vault)
    await mgr.capture(world.uid, "ws://x", "swiggy.com")
    assert await mgr.revoke(world.uid, "swiggy.com") is False and await mgr.sites(world.uid) == set()


# ───────────── the HTTP routes ─────────────

@pytest.fixture
async def api(world):
    world.monkeypatch.setattr(rt, "_runtime", SimpleNamespace(vault=world.vault, sessions=world.mgr))
    app = FastAPI()
    app.include_router(router)

    async def who(request: Request):
        uid = request.headers.get("x-test-user")
        return SimpleNamespace(metadata={"user_id": uid}) if uid else None
    app.dependency_overrides[get_current_user] = who
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        world.c = c
        yield world


def _as(w):
    return {**H, "x-test-user": w.uid}


async def test_revoking_a_saved_login_also_signs_out_of_that_site(api):
    cred = await api.vault.store(user_id=api.uid, kind="password", label="Swiggy", origins=["swiggy.com"], secret={"username": "me", "password": "pw-pw-pw-1"})
    await api.mgr.capture(api.uid, "ws://x", "swiggy.com")
    r = await api.c.delete(f"/chainlit/vault/credentials/{cred.id}", headers=_as(api))
    assert r.status_code == 200 and r.json() == {"revoked": True, "signed_out": ["swiggy.com"], "browser_cleared": True}
    assert api.cleared == [("ws://profile", "swiggy.com")] and await api.mgr.sites(api.uid) == set()


async def test_a_busy_browser_is_reported_not_hidden(api):
    cred = await api.vault.store(user_id=api.uid, kind="password", label="Swiggy", origins=["swiggy.com"], secret={"username": "me", "password": "pw-pw-pw-1"})
    api.prov.busy = True
    r = await api.c.delete(f"/chainlit/vault/credentials/{cred.id}", headers=_as(api))
    assert r.json()["revoked"] is True and r.json()["browser_cleared"] is False


async def test_revoking_an_api_key_does_not_touch_any_session(api):
    cred = await api.vault.store(user_id=api.uid, kind="api_key", label="K", origins=["swiggy.com"], secret={"key": "k" * 24})
    await api.mgr.capture(api.uid, "ws://x", "swiggy.com")
    r = await api.c.delete(f"/chainlit/vault/credentials/{cred.id}", headers=_as(api))
    assert r.json() == {"revoked": True} and api.cleared == [] and await api.mgr.sites(api.uid) == {"swiggy.com"}


async def test_another_users_credential_cannot_be_used_to_sign_a_user_out(api):
    other = (await api.svc.tenancy.ensure_user(google_sub="g2", email="b@x.com", name="B")).id
    cred = await api.vault.store(user_id=other, kind="password", label="Swiggy", origins=["swiggy.com"], secret={"username": "me", "password": "pw-pw-pw-1"})
    r = await api.c.delete(f"/chainlit/vault/credentials/{cred.id}", headers=_as(api))
    assert r.status_code == 404 and api.cleared == []


async def test_sign_out_and_forget_reports_whether_the_browser_was_cleared(api):
    r = await api.c.delete("/chainlit/vault/sessions/swiggy.com", headers=_as(api))
    assert r.json() == {"revoked": True, "browser_cleared": True} and api.cleared == [("ws://profile", "swiggy.com")]
