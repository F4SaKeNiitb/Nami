"""Vault HTTP routes: the only path a secret travels. Auth, CSRF, no echo, per-user isolation, revocation."""

import logging
from types import SimpleNamespace

import httpx
import pyotp
import pytest
from chainlit.auth import get_current_user
from fastapi import FastAPI, Request
from sqlalchemy import select

import app.core.runtime as rt
from app.browser.session_manager import SessionManager
from app.db.models import Credential
from app.vault.kms import LocalKms
from app.vault.router import router
from app.vault.service import VaultService

PW = "Tr0ub4dor&3-unique-marker"
SEED = pyotp.random_base32(32)
H = {"X-Requested-With": "nami"}


@pytest.fixture
async def world(svc, monkeypatch):
    vault = VaultService(svc.db, LocalKms())
    monkeypatch.setattr(rt, "_runtime", SimpleNamespace(vault=vault, sessions=SessionManager(svc.db, vault)))
    app = FastAPI()
    app.include_router(router)

    async def who(request: Request):       # test stand-in for Chainlit's cookie auth: header names the user, none = anonymous
        uid = request.headers.get("x-test-user")
        return SimpleNamespace(metadata={"user_id": uid}) if uid else None
    app.dependency_overrides[get_current_user] = who
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield SimpleNamespace(c=c, db=svc.db, vault=vault)


def as_(uid, **extra):
    return {"x-test-user": uid, **extra}


async def make_user(svc, n):
    return (await svc.tenancy.ensure_user(google_sub=f"g-{n}", email=f"{n}@x.com", name=n)).id


NEW = {"kind": "password", "label": "Swiggy", "origins": ["swiggy.com"], "secret": {"username": "me", "password": PW}}


async def test_every_route_needs_a_signed_in_user(world):
    for method, path in [("GET", "/ping"), ("GET", "/credentials"), ("POST", "/credentials"), ("GET", "/log"),
                         ("DELETE", "/credentials/abc"), ("GET", "/sessions"), ("POST", "/totp/preview")]:
        r = await world.c.request(method, "/chainlit/vault" + path, headers=H, json={})
        assert r.status_code == 401, (method, path)


@pytest.mark.parametrize("headers", [{}, {"X-Requested-With": "other"},
                                     {"X-Requested-With": "nami", "Origin": "https://evil.example"}])
async def test_state_changing_calls_reject_csrf(svc, world, headers):
    uid = await make_user(svc, "a")
    for method, path in [("POST", "/credentials"), ("DELETE", "/credentials/x"), ("POST", "/totp/preview"),
                         ("DELETE", "/sessions/swiggy.com")]:
        r = await world.c.request(method, "/chainlit/vault" + path, headers=as_(uid, **headers), json=NEW)
        assert r.status_code == 403, (method, path, headers)
    assert await world.vault.list(uid) == []


async def test_same_origin_browser_call_is_accepted(svc, world):
    uid = await make_user(svc, "a")
    r = await world.c.post("/chainlit/vault/credentials", json=NEW,
                           headers=as_(uid, **H, Origin="http://test"))
    assert r.status_code == 200


async def test_secret_is_never_echoed_or_listed(svc, world, caplog):
    caplog.set_level(logging.DEBUG)
    uid = await make_user(svc, "a")
    r = await world.c.post("/chainlit/vault/credentials", json=NEW, headers=as_(uid, **H))
    assert r.status_code == 200 and PW not in r.text
    listing = await world.c.get("/chainlit/vault/credentials", headers=as_(uid))
    log = await world.c.get("/chainlit/vault/log", headers=as_(uid))
    assert PW not in listing.text + log.text and PW not in caplog.text
    assert listing.json()[0]["label"] == "Swiggy"


async def test_validation_error_does_not_echo_the_secret(svc, world):
    uid = await make_user(svc, "a")
    bad = {"kind": "password", "label": "x" * 500, "secret": {"password": PW}}     # label too long → 422
    r = await world.c.post("/chainlit/vault/credentials", json=bad, headers=as_(uid, **H))
    assert r.status_code in (400, 422)
    assert PW not in r.text


async def test_other_users_credential_looks_missing_and_survives(svc, world):
    a, b = await make_user(svc, "a"), await make_user(svc, "b")
    cid = (await world.c.post("/chainlit/vault/credentials", json=NEW, headers=as_(a, **H))).json()["id"]
    assert (await world.c.get("/chainlit/vault/credentials", headers=as_(b))).json() == []
    r = await world.c.delete(f"/chainlit/vault/credentials/{cid}", headers=as_(b, **H))
    assert r.status_code == 404
    assert len(await world.vault.list(a)) == 1           # not revoked by the attacker


async def test_revoke_wipes_ciphertext(svc, world):
    uid = await make_user(svc, "a")
    cid = (await world.c.post("/chainlit/vault/credentials", json=NEW, headers=as_(uid, **H))).json()["id"]
    r = await world.c.delete(f"/chainlit/vault/credentials/{cid}", headers=as_(uid, **H))
    assert r.status_code == 200
    async with world.db.session() as s:
        row = (await s.scalars(select(Credential).where(Credential.id == cid))).one()
        assert row.revoked_at is not None and not row.ciphertext.strip(b"\x00")
    assert (await world.c.get("/chainlit/vault/credentials", headers=as_(uid))).json() == []


async def test_invalid_totp_seed_is_a_400_not_a_500(svc, world):
    uid = await make_user(svc, "a")
    r = await world.c.post("/chainlit/vault/credentials", headers=as_(uid, **H),
                           json={"kind": "totp", "label": "t", "origins": ["x.com"], "secret": {"seed": "short"}})
    assert r.status_code == 400
    p = await world.c.post("/chainlit/vault/totp/preview", headers=as_(uid, **H), json={"seed": "nope"})
    assert p.status_code == 400


async def test_totp_preview_returns_code_and_stores_nothing(svc, world):
    uid = await make_user(svc, "a")
    r = await world.c.post("/chainlit/vault/totp/preview", headers=as_(uid, **H), json={"seed": SEED})
    assert r.status_code == 200 and r.json()["code"] == pyotp.TOTP(SEED).now()
    assert SEED not in r.text
    assert await world.vault.list(uid) == []


async def test_access_log_is_per_user(svc, world):
    a, b = await make_user(svc, "a"), await make_user(svc, "b")
    await world.c.post("/chainlit/vault/credentials", json=NEW, headers=as_(a, **H))
    assert (await world.c.get("/chainlit/vault/log", headers=as_(b))).json() == []
    assert (await world.c.get("/chainlit/vault/log", headers=as_(a))).json() != []


async def test_activity_is_plain_language_per_user_and_filterable(svc, world):
    a, b = await make_user(svc, "a"), await make_user(svc, "b")
    cid = (await world.c.post("/chainlit/vault/credentials", json=NEW, headers=as_(a, **H))).json()["id"]
    await world.vault.log_request(user_id=a, credential_id=cid, actor="agent:executor", task_id="t", host="x.com",
                                  method="POST", path="/v1/things?secret=1", status=402)
    items = (await world.c.get("/chainlit/vault/activity", headers=as_(a))).json()
    assert items[0]["text"].endswith("POST /v1/things (the account is out of credits)") and items[0]["ok"] is False
    assert any("saved" in i["text"] for i in items) and "secret=1" not in repr(items)
    assert (await world.c.get("/chainlit/vault/activity", headers=as_(b))).json() == []
    assert (await world.c.get(f"/chainlit/vault/activity?credential_id={cid}", headers=as_(b))).json() == []   # another user's id shows nothing
    assert (await world.c.get("/chainlit/vault/activity")).status_code == 401


async def test_activity_still_shows_a_removed_item_by_name(svc, world):
    a = await make_user(svc, "a")
    cid = (await world.c.post("/chainlit/vault/credentials", json=NEW, headers=as_(a, **H))).json()["id"]
    await world.c.delete(f"/chainlit/vault/credentials/{cid}", headers=as_(a, **H))
    items = (await world.c.get("/chainlit/vault/activity", headers=as_(a))).json()
    assert items[0]["text"].startswith("You removed") and NEW["label"] in items[0]["text"]
