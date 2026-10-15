"""Task ledger routes: per-user, scrubbed, bounded; cancel is owner-only and never rewrites a finished task."""

from types import SimpleNamespace

import httpx
import pytest
from chainlit.auth import get_current_user
from fastapi import FastAPI, Request

import app.core.runtime as rt
from app.db.models import Task, TaskEvent
from app.tasks_router import router
from app.tenancy.service import PermissionDenied

H = {"X-Requested-With": "nami"}


class FakeOrch:
    def __init__(self, svc):
        self.svc, self.cancelled = svc, []

    async def cancel(self, task_id, *, user_id):
        async with self.svc.db.session() as s:
            t = await s.get(Task, task_id)
            if not t or t.user_id != user_id:
                raise PermissionDenied("no")
            t.status = "cancelled"
        self.cancelled.append(task_id)


@pytest.fixture
async def w(svc, monkeypatch):
    a = await svc.tenancy.ensure_user(google_sub="g1", email="a@x.com", name="A")
    b = await svc.tenancy.ensure_user(google_sub="g2", email="b@x.com", name="B")
    org = await svc.tenancy.create_org(name="Acme", owner_id=a.id)
    orch = FakeOrch(svc)
    monkeypatch.setattr(rt, "_runtime", SimpleNamespace(services=svc, orchestrator=orch))
    app = FastAPI()
    app.include_router(router)

    async def who(request: Request):
        u = request.headers.get("x-test-user")
        return SimpleNamespace(metadata={"user_id": u, "org_id": org.id}) if u else None
    app.dependency_overrides[get_current_user] = who
    async with svc.db.session() as s:
        t = Task(org_id=org.id, user_id=a.id, request_text="order lunch", status="running")
        s.add(t)
        await s.flush()
        tid = t.id
        s.add(TaskEvent(task_id=tid, type="plan", payload={"subgoals": [{"goal": "Open menu", "irreversible": False}]}))
        s.add(TaskEvent(task_id=tid, type="finished", payload={"screenshot_b64": "AAAA", "note": "key sk-or-v1-abcdef0123456789abcdef0123456789"}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield SimpleNamespace(c=c, a=a, b=b, tid=tid, orch=orch, db=svc.db)


def as_(u, **k):
    return {"x-test-user": u.id, **k}


async def test_requires_sign_in(w):
    assert (await w.c.get("/chainlit/tasks")).status_code == 401


async def test_lists_only_my_tasks_and_reads_one(w):
    assert [t["id"] for t in (await w.c.get("/chainlit/tasks", headers=as_(w.a))).json()] == [w.tid]
    assert (await w.c.get("/chainlit/tasks", headers=as_(w.b))).json() == []
    r = (await w.c.get(f"/chainlit/tasks/{w.tid}", headers=as_(w.a))).json()
    assert r["plan"] == [{"goal": "Open menu", "irreversible": False}] and [e["type"] for e in r["events"]] == ["plan", "finished"]


async def test_another_users_task_is_not_found(w):
    assert (await w.c.get(f"/chainlit/tasks/{w.tid}", headers=as_(w.b))).status_code == 404
    assert (await w.c.post(f"/chainlit/tasks/{w.tid}/cancel", headers=as_(w.b, **H))).status_code == 404
    assert w.orch.cancelled == []


async def test_events_are_redacted_and_screenshots_dropped(w):
    body = (await w.c.get(f"/chainlit/tasks/{w.tid}", headers=as_(w.a))).text
    assert "sk-or-v1-abcdef" not in body and "AAAA" not in body


async def test_cancel_needs_csrf_header_and_works_for_owner(w):
    assert (await w.c.post(f"/chainlit/tasks/{w.tid}/cancel", headers=as_(w.a))).status_code == 403
    assert (await w.c.post(f"/chainlit/tasks/{w.tid}/cancel", headers=as_(w.a, **H))).json() == {"cancelled": True}
    assert w.orch.cancelled == [w.tid]


async def test_cancel_does_not_rewrite_a_finished_task(w):
    async with w.db.session() as s:
        (await s.get(Task, w.tid)).status = "done"
    r = (await w.c.post(f"/chainlit/tasks/{w.tid}/cancel", headers=as_(w.a, **H))).json()
    assert r["cancelled"] is False and w.orch.cancelled == []
