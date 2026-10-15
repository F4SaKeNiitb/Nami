"""Replay a finished task from its stored event log after a prompt or model change.

What can be replayed: the PLANNING decision. The event log keeps the intent (goal, sites) and the plan that was made, so the
same intent can be put through a different planner prompt or model and the new subgoals compared with the old ones. What
cannot: the browser steps (they depend on live pages) and the access plan's login state (it is not in the log, so it is
rebuilt as `login="none"` unless the caller supplies one). The result says so; it is a comparison of plans, not a re-run.

Pure apart from the model call, which is injected, so a scripted model works in tests.
"""

from dataclasses import dataclass, field

from sqlalchemy import select

from app.agent.planner import make_plan
from app.agent.schemas import AccessPlan, Intent, Plan
from app.db.engine import Database
from app.db.models import TaskEvent

DECISIONS = ("retry", "replan", "approval", "approved", "gateway_block", "session_expired", "need_credentials", "budget_exhausted")


@dataclass
class Recorded:
    goal: str = ""
    sites: list[str] = field(default_factory=list)
    plan: list[dict] = field(default_factory=list)
    validations: list[bool] = field(default_factory=list)
    decisions: dict[str, int] = field(default_factory=dict)


async def load_events(db: Database, task_id: str) -> list[tuple[str, dict]]:
    async with db.session() as s:
        rows = (await s.scalars(select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.ts, TaskEvent.id))).all()
        return [(r.type, dict(r.payload or {})) for r in rows]


def recorded(events: list[tuple[str, dict]]) -> Recorded:
    r = Recorded()
    for type_, p in events:
        if type_ == "intent":
            r.goal, r.sites = str(p.get("goal", "")), list(p.get("sites") or [])
        elif type_ == "plan":
            r.plan = [g for g in (p.get("subgoals") or []) if isinstance(g, dict)]      # the LAST plan wins (a replan overwrites)
        elif type_ == "validated":
            r.validations.append(bool(p.get("passed")))
        if type_ in DECISIONS:
            r.decisions[type_] = r.decisions.get(type_, 0) + 1
    return r


def diff_plans(old: list[dict], new: list[dict]) -> dict:
    o = [str(g.get("goal", "")).strip().lower() for g in old]
    n = [str(g.get("goal", "")).strip().lower() for g in new]
    irr_old = {g for g, d in zip(o, old) if d.get("irreversible")}
    irr_new = {g for g, d in zip(n, new) if d.get("irreversible")}
    return {"same": [g for g in n if g in o], "added": [g for g in n if g not in o], "removed": [g for g in o if g not in n],
            "steps_before": len(o), "steps_after": len(n),
            "irreversible_changed": irr_old != irr_new,
            "irreversible_before": sorted(irr_old), "irreversible_after": sorted(irr_new)}


async def replay_planning(events: list[tuple[str, dict]], llm, *, model: str | None = None, rules_block: str = "",
                          access: AccessPlan | None = None) -> dict:
    rec = recorded(events)
    if not rec.goal or not rec.sites or not rec.plan:
        return {"replayable": False, "why": "the log has no intent and plan for this task"}
    intent = Intent(goal=rec.goal, sites=rec.sites)
    acc = access or AccessPlan(allowed_domains=rec.sites)
    new: Plan = await make_plan(llm, intent, acc, rules_block=rules_block, model=model)
    return {"replayable": True, "goal": rec.goal, "recorded_decisions": rec.decisions,
            "diff": diff_plans(rec.plan, [g.model_dump() for g in new.subgoals]),
            "note": "planning only: browser steps and the login state are not replayed"}
