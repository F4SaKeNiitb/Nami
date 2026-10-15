"""Roles gate what a user may do to their own credentials. The credentials themselves stay personal. Written, not run."""

from types import SimpleNamespace

import httpx
import pytest
from chainlit.auth import get_current_user
from fastapi import FastAPI, Request
from sqlalchemy import select

import app.core.runtime as rt
from app.browser.session_manager import SessionManager
from app.db.models import Membership
from app.vault.kms import LocalKms
from app.vault.router import router
from app.vault.service import VaultService

H = {"X-Requested-With": "nami"}
NEW = {"kind": "password", "label": "Swiggy", "origins": ["swiggy.com"], "secret": {"username": "me", "password": "Tr0ub4dor&3-marker"}}


@pytest.fixture
async def world(svc, monkeypatch):
    vault = VaultService(svc.db, LocalKms())
    monkeypatch.setattr(rt, "_runtime", SimpleNamespace(vault=vault, sessions=SessionManager(svc.db, vault),
                                                        services=svc))
    app = FastAPI()
    app.include_router(router)

    async def who(request: Request):
        uid = request.headers.get("x-test-user")
        return SimpleNamespace(metadata={"user_id": uid}) if uid else None
    app.dependency_overrides[get_current_user] = who
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield SimpleNamespace(c=c, svc=svc, vault=vault)


async def user_with_role(svc, name, role):
    """A user whose DEFAULT org has `role` for them (a personal org starts as owner; change it to test other roles)."""
    u = await svc.tenancy.ensure_user(google_sub=f"g-{name}", email=f"{name}@x.com", name=name)
    async with svc.db.session() as s:
        m = await s.scalar(select(Membership).where(Membership.org_id == u.default_org_id, Membership.user_id == u.id))
        if role is None:
            await s.delete(m)
        else:
            m.role = role
    return u.id


@pytest.mark.parametrize("role,allowed", [("owner", True), ("admin", True), ("member", True), ("viewer", False),
                                          (None, True)])          # None = no membership row: today's behaviour
async def test_saving_a_credential_by_role(world, role, allowed):
    uid = await user_with_role(world.svc, f"u-{role}", role)
    r = await world.c.post("/chainlit/vault/credentials", headers={**H, "x-test-user": uid}, json=NEW)
    assert r.status_code == (200 if allowed else 403), r.text
    assert len(await world.vault.list(uid)) == (1 if allowed else 0)


async def test_a_viewer_can_still_revoke_and_read_their_own_items(world):
    uid = await user_with_role(world.svc, "v", "member")
    cid = (await world.c.post("/chainlit/vault/credentials", headers={**H, "x-test-user": uid}, json=NEW)).json()["id"]
    await user_with_role(world.svc, "v", "viewer")                 # demoted afterwards
    assert (await world.c.get("/chainlit/vault/credentials", headers={"x-test-user": uid})).status_code == 200
    assert (await world.c.get("/chainlit/vault/activity", headers={"x-test-user": uid})).status_code == 200
    # removing your own access is a safety action: open to every role
    assert (await world.c.delete(f"/chainlit/vault/credentials/{cid}", headers={**H, "x-test-user": uid})).status_code == 200


async def test_an_admin_cannot_see_or_revoke_someone_elses_credentials(world):
    owner = await user_with_role(world.svc, "boss", "owner")
    member = await user_with_role(world.svc, "emp", "member")
    cid = (await world.c.post("/chainlit/vault/credentials", headers={**H, "x-test-user": member}, json=NEW)).json()["id"]
    assert (await world.c.get("/chainlit/vault/credentials", headers={"x-test-user": owner})).json() == []
    assert (await world.c.delete(f"/chainlit/vault/credentials/{cid}", headers={**H, "x-test-user": owner})).status_code == 404
    assert len(await world.vault.list(member)) == 1


async def test_a_denied_save_is_logged_by_authz(world):
    from app.db.models import AuthzLog
    uid = await user_with_role(world.svc, "vv", "viewer")
    await world.c.post("/chainlit/vault/credentials", headers={**H, "x-test-user": uid}, json=NEW)
    async with world.svc.db.session() as s:
        rows = (await s.scalars(select(AuthzLog).where(AuthzLog.user_id == uid, AuthzLog.action == "create_credential"))).all()
    assert len(rows) == 1 and rows[0].allowed is False and "viewer" in rows[0].reason
