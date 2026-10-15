"""Operator view: how the agent is doing. Counts and rates only, never content, secrets or page text."""

from datetime import timedelta

from chainlit.auth import get_current_user, reuseable_oauth
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select

from app.core.runtime import runtime
from app.db.base import utcnow
from app.db.models import SecretAccessLog, Task, TaskEvent
from app.telemetry import tel

router = APIRouter(prefix="/admin", tags=["admin"])


async def _user_or_none(request: Request):
    """Chainlit's own dependency answers 401 to anonymous callers, which would show that this page exists."""
    try:
        return await get_current_user(await reuseable_oauth(request))
    except HTTPException:
        return None


async def admin_only(user=Depends(_user_or_none)) -> str:
    allowed = {e.strip().lower() for e in runtime().settings.admin_emails.split(",") if e.strip()}
    if not user or user.identifier.lower() not in allowed:
        raise HTTPException(404, "not found")            # do not reveal that the page exists
    return user.identifier


@router.get("/metrics")
async def metrics(_: str = Depends(admin_only)):
    rt = runtime()
    since = utcnow() - timedelta(hours=24)
    async with rt.services.db.session() as s:
        by_status = dict((await s.execute(select(Task.status, func.count()).group_by(Task.status))).all())
        recent = (await s.scalar(select(func.count()).select_from(Task).where(Task.created_at >= since))) or 0
        events = dict((await s.execute(select(TaskEvent.type, func.count()).where(TaskEvent.ts >= since)
                                       .group_by(TaskEvent.type))).all())
        vault = dict((await s.execute(select(SecretAccessLog.result, func.count()).where(SecretAccessLog.ts >= since)
                                      .group_by(SecretAccessLog.result))).all())
        denied_why = dict((await s.execute(select(SecretAccessLog.reason, func.count()).where(
            SecretAccessLog.result == "denied", SecretAccessLog.ts >= since).group_by(SecretAccessLog.reason))).all())
        costs = [c for c in (await s.scalars(select(Task.cost).where(Task.cost.is_not(None)))).all() if c]
        results = [r for r in (await s.scalars(select(Task.result).where(Task.status.in_(["done", "unverified"])))).all() if r]
        step_counts = [n for (n,) in (await s.execute(select(func.count()).select_from(TaskEvent).where(
            TaskEvent.type == "validated").group_by(TaskEvent.task_id))).all()]
    finished = by_status.get("done", 0) + by_status.get("unverified", 0) + by_status.get("failed", 0) + by_status.get("blocked", 0)
    return {
        "tasks": {"by_status": by_status, "last_24h": recent,
                  "verified_rate": round(by_status.get("done", 0) / finished, 3) if finished else None},
        "agent_events_24h": {k: events.get(k, 0) for k in ("retry", "replan", "approval", "approved", "gateway_block",
                                                           "session_expired", "takeover")},
        "vault_24h": {"allowed": vault.get("allowed", 0), "denied": vault.get("denied", 0), "denied_reasons": denied_why},
        "llm": dict(rt.llm.usage),
        "cost_per_task": _cost(costs, rt.settings),
        "steps_per_task": round(sum(step_counts) / len(step_counts), 2) if step_counts else None,
        "verifier_confidence": _histogram([r.get("confidence") for r in results if isinstance(r.get("confidence"), (int, float))]),
        "cache": {ns: {**d, "hit_ratio": round(d["hits"] / (d["hits"] + d["misses"]), 3) if d["hits"] + d["misses"] else None}
                  for ns, d in rt.services.cache.stats.items()},
        "counters": {f"{n}{{{','.join(f'{k}={v}' for k, v in lbl)}}}": c for (n, lbl), c in sorted(tel.counters.items())},
    }


def _cost(costs: list[dict], settings) -> dict:
    if not costs:
        return {"tasks": 0}
    n = len(costs)
    tin, tout = sum(c.get("input_tokens", 0) for c in costs), sum(c.get("output_tokens", 0) for c in costs)
    usd = (tin * settings.price_in_per_mtok + tout * settings.price_out_per_mtok) / 1_000_000
    return {"tasks": n, "avg_input_tokens": round(tin / n), "avg_output_tokens": round(tout / n),
            "avg_calls": round(sum(c.get("calls", 0) for c in costs) / n, 1), "est_usd_per_task": round(usd / n, 4)}


def _histogram(values: list[float]) -> dict[str, int]:
    buckets = {"<0.5": 0, "0.5-0.7": 0, "0.7-0.9": 0, ">=0.9": 0}
    for v in values:
        buckets["<0.5" if v < 0.5 else "0.5-0.7" if v < 0.7 else "0.7-0.9" if v < 0.9 else ">=0.9"] += 1
    return buckets
