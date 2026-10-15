"""/admin/metrics must look the same to anonymous callers as a page that does not exist (404, never Chainlit's 401)."""

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

import app.core.runtime as rt
from app.admin import router


@pytest.fixture
async def real_auth_client(monkeypatch):
    """No dependency override: the real Chainlit auth runs, with login required and no cookie."""
    import chainlit.auth as ca
    monkeypatch.setattr(ca, "require_login", lambda: True)
    monkeypatch.setattr(rt, "_runtime", SimpleNamespace(settings=SimpleNamespace(admin_emails="boss@x.com")))
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_anonymous_caller_gets_404_not_401(real_auth_client):
    r = await real_auth_client.get("/admin/metrics")
    assert r.status_code == 404


async def test_bad_token_gets_404_not_401(real_auth_client):
    r = await real_auth_client.get("/admin/metrics", headers={"Authorization": "Bearer not-a-token"})
    assert r.status_code == 404
