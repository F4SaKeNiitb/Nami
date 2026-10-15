"""Task ledger over HTTP: list my tasks, read one task's record, cancel it. Same cookie auth and CSRF guard as the vault.

Read-only except cancel. Resuming a paused task (approvals, codes, sign-in) stays in the chat on purpose: an approval has to
be given where the user sees the exact card, so there is no HTTP route that approves anything.
Everything returned was already scrubbed when it was stored; it is redacted again and bounded here.
"""

from types import SimpleNamespace
from typing import Any

from chainlit.auth import get_current_user
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from app.core.runtime import runtime
from app.db.models import Task, TaskEvent
from app.db.scoped import ScopedDatabase
from app.security.secrets import redact
from app.tenancy.access import access
from app.tenancy.service import PermissionDenied
from app.vault.router import same_origin

router = APIRouter(prefix="/chainlit/tasks", tags=["tasks"])

async def who(request: Request, user=Depends(get_current_user)) -> tuple[str, str]:
    """(user id, the organisation the session is signed in to). Everything below is limited to that organisation."""
    meta = (getattr(user, "metadata", None) or {}) if user else {}
    uid = meta.get("user_id")
    if not uid:
        raise HTTPException(401, "sign in first")
    org = meta.get("org_id") or await access().default_org(uid)
    if not org:
        raise HTTPException(404, "not found")
    return uid, org


def _scoped(ident: tuple[str, str]):
    return ScopedDatabase(runtime().services.db, ident[1]).session()


MAX_LIST = 30
MAX_EVENTS = 300
_MAX_STR = 400
_FINISHED = {"done", "unverified", "failed", "cancelled"}


def _safe(v: Any, depth: int = 0) -> Any:
    """Strings redacted and cut, bulky blobs (screenshots, long text) dropped, nesting bounded."""
    if isinstance(v, str):
        return redact(v)[:_MAX_STR]
    if isinstance(v, dict) and depth < 3:
        return {str(k): _safe(x, depth + 1) for k, x in list(v.items())[:30]
                if not (isinstance(x, str) and len(x) > 4000) and "screenshot" not in str(k) and "b64" not in str(k)}
    if isinstance(v, list) and depth < 3:
        return [_safe(x, depth + 1) for x in v[:30]]
    return v if isinstance(v, (int, float, bool)) or v is None else None


def _result(r: dict | None) -> dict | None:
    return None if r is None else _safe(r)


def _row(t: Task) -> dict:
    return {"id": t.id, "request": redact(t.request_text)[:200], "status": t.status,
            "created_at": t.created_at.isoformat(), "updated_at": t.updated_at.isoformat()}


@router.get("")
async def list_tasks(ident=Depends(who)):
    uid = ident[0]
    async with _scoped(ident) as s:
        rows = (await s.scalars(select(Task).where(Task.user_id == uid).order_by(Task.created_at.desc()).limit(MAX_LIST))).all()
        return [_row(t) for t in rows]


@router.get("/{task_id}")
async def get_task(task_id: str, ident=Depends(who)):
    uid = ident[0]
    async with _scoped(ident) as s:
        t = await s.scalar(select(Task).where(Task.id == task_id))
        head = None if t is None else (SimpleNamespace(id=t.id, org_id=t.org_id, user_id=t.user_id), _row(t), _result(t.result),
                                       _safe(t.cost) if t.cost else None)
    # the permission check opens its own session, and the pool holds one connection: never nest it inside the one above
    if head is None or not await access().can_read_task(uid, head[0]):
        raise HTTPException(404, "not found")                 # the same answer for "missing" and "not yours"
    async with _scoped(ident) as s:
        events = (await s.scalars(select(TaskEvent).where(TaskEvent.task_id == task_id)
                                  .order_by(TaskEvent.ts, TaskEvent.id).limit(MAX_EVENTS))).all()
        plan = next((e.payload.get("subgoals") for e in reversed(events) if e.type == "plan"), None)
        return {**head[1], "result": head[2], "cost": head[3],
                "plan": [{"goal": redact(str(g.get("goal", "")))[:200], "irreversible": bool(g.get("irreversible"))}
                         for g in (plan or []) if isinstance(g, dict)],
                "events": [{"type": e.type, "ts": e.ts.isoformat(), "payload": _safe(e.payload)} for e in events]}


@router.post("/{task_id}/cancel", dependencies=[Depends(same_origin)])
async def cancel_task(task_id: str, ident=Depends(who)):
    uid = ident[0]
    async with _scoped(ident) as s:
        t = await s.scalar(select(Task).where(Task.id == task_id))
    if t is not None and t.user_id == uid and t.status in _FINISHED:
        return {"cancelled": False, "status": t.status}        # a finished task keeps its real outcome
    try:
        await runtime().orchestrator.cancel(task_id, user_id=uid)
    except PermissionDenied:
        raise HTTPException(404, "not found") from None
    return {"cancelled": True}
