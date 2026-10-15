"""Admin metrics gate, Chainlit data-layer schema drift, and Alembic migrations."""

import os
import sqlite3
import subprocess
import sys
import uuid
from types import SimpleNamespace

import httpx
import pytest
from chainlit.auth import get_current_user
from fastapi import FastAPI, Request
from sqlalchemy import create_engine, inspect

import app.core.runtime as rt
from app.admin import _user_or_none, router
from app.chainlit_schema import ensure_chainlit_schema


# ───────────── A13: admin ─────────────

@pytest.fixture
async def admin_client(svc, monkeypatch):
    monkeypatch.setattr(rt, "_runtime", SimpleNamespace(
        settings=SimpleNamespace(admin_emails="boss@x.com", price_in_per_mtok=2.0, price_out_per_mtok=10.0), services=svc, llm=SimpleNamespace(usage={"calls": 1})))
    app = FastAPI()
    app.include_router(router)

    async def who(request: Request):
        e = request.headers.get("x-email")
        return SimpleNamespace(identifier=e) if e else None
    app.dependency_overrides[_user_or_none] = who
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_non_admins_and_anonymous_get_404(admin_client):
    assert (await admin_client.get("/admin/metrics")).status_code == 404
    assert (await admin_client.get("/admin/metrics", headers={"x-email": "user@x.com"})).status_code == 404
    assert (await admin_client.get("/admin/metrics", headers={"x-email": "boss@x.com.evil.io"})).status_code == 404


async def test_admin_sees_counts_only(admin_client, svc):
    r = await admin_client.get("/admin/metrics", headers={"x-email": "BOSS@x.com"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"tasks", "agent_events_24h", "vault_24h", "llm", "counters", "cost_per_task", "steps_per_task",
                         "verifier_confidence", "cache"}
    assert "request" not in r.text and "secret" not in r.text.replace("vault_24h", "")


async def test_cost_confidence_and_cache_metrics_aggregate_without_content(admin_client, svc):
    from app.db.models import Task
    async with svc.db.session() as s:
        u = await svc.tenancy.ensure_user(google_sub="g", email="a@x.com", name="A")
        for i, (conf, cost) in enumerate([(0.95, 1000), (0.4, 3000)]):
            s.add(Task(org_id=u.default_org_id, user_id=u.id, request_text="SECRET-REQUEST-TEXT", status="done",
                       result={"kind": "result", "confidence": conf, "summary": "SECRET-SUMMARY"},
                       cost={"input_tokens": cost, "output_tokens": 100, "calls": 4}))
    await svc.cache.get_json("llm", "k"); await svc.cache.set_json("llm", "k", {"a": 1}); await svc.cache.get_json("llm", "k")
    r = await admin_client.get("/admin/metrics", headers={"x-email": "boss@x.com"})
    body = r.json()
    assert body["verifier_confidence"] == {"<0.5": 1, "0.5-0.7": 0, "0.7-0.9": 0, ">=0.9": 1}
    assert body["cost_per_task"]["avg_input_tokens"] == 2000 and body["cost_per_task"]["tasks"] == 2
    assert body["cache"]["llm"]["hit_ratio"] == 0.5
    assert "SECRET-REQUEST-TEXT" not in r.text and "SECRET-SUMMARY" not in r.text


# ───────────── A14: Chainlit schema drift ─────────────

async def test_chainlit_data_layer_can_write_every_object_type(tmp_path, monkeypatch):
    """Drives the real SQLAlchemyDataLayer against our DDL. A missing column makes the write fail (the autoCollapse class of bug)."""
    from chainlit.context import context_var
    from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
    token = context_var.set(SimpleNamespace(session=SimpleNamespace()))   # data layer only needs a non-websocket session
    path = tmp_path / "cl.db"
    ensure_chainlit_schema(str(path))
    dl = SQLAlchemyDataLayer(conninfo=f"sqlite+aiosqlite:///{path}")
    user = await dl.create_user(SimpleNamespace(identifier="a@x.com", metadata={"user_id": "u"}, display_name="A"))
    assert user is not None
    tid, sid = str(uuid.uuid4()), str(uuid.uuid4())
    await dl.update_thread(thread_id=tid, name="t", user_id=user.id)
    await dl.create_step({"id": sid, "threadId": tid, "name": "n", "type": "run", "output": "o", "createdAt": "2026-01-01T00:00:00Z",
                          "autoCollapse": True, "modes": None, "icon": "x", "streaming": False})
    thread = await dl.get_thread(tid)
    assert thread and [s["id"] for s in thread["steps"]] == [sid]
    context_var.reset(token)


def test_ensure_schema_upgrades_an_old_file_in_place(tmp_path):
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE users ("id" TEXT PRIMARY KEY, "identifier" TEXT NOT NULL UNIQUE, "metadata" TEXT NOT NULL, "createdAt" TEXT)')
    con.execute('CREATE TABLE threads ("id" TEXT PRIMARY KEY, "createdAt" TEXT, "name" TEXT, "userId" TEXT, "userIdentifier" TEXT, "tags" TEXT, "metadata" TEXT)')
    con.execute('CREATE TABLE steps ("id" TEXT PRIMARY KEY, "name" TEXT NOT NULL, "type" TEXT NOT NULL, "threadId" TEXT NOT NULL, "streaming" INTEGER NOT NULL)')
    con.commit(); con.close()
    ensure_chainlit_schema(path)
    ensure_chainlit_schema(path)                                   # idempotent
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(steps)")}
    assert {"autoCollapse", "modes", "icon"} <= cols


# ───────────── A15: migrations ─────────────

def _run(args, db):
    env = {**os.environ, "PYTHONPATH": ".", "NAMI_DATABASE_URL": f"sqlite+aiosqlite:///{db}"}
    return subprocess.run([sys.executable, "-m", "alembic", *args], capture_output=True, text=True, env=env, timeout=120)


def test_upgrade_head_matches_models_and_downgrade_round_trips(tmp_path):
    db = tmp_path / "m.db"
    up = _run(["upgrade", "head"], db)
    assert up.returncode == 0, up.stderr[-1500:]
    chk = _run(["check"], db)
    assert chk.returncode == 0, chk.stdout + chk.stderr          # models and migrations agree
    from app.db.base import Base
    import app.db.models  # noqa: F401
    have = set(inspect(create_engine(f"sqlite:///{db}")).get_table_names()) - {"alembic_version"}
    assert have == set(Base.metadata.tables)
    assert _run(["downgrade", "base"], db).returncode == 0
    assert _run(["upgrade", "head"], db).returncode == 0
