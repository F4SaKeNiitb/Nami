"""Replay of a stored task's planning decision with a scripted model."""

from app.agent.schemas import Plan, Subgoal
from app.eval import replay

EVENTS = [
    ("intent", {"goal": "reorder my last order", "sites": ["foodco.com"]}),
    ("plan", {"subgoals": [{"goal": "Open orders", "irreversible": False}, {"goal": "Place the order", "irreversible": True}]}),
    ("retry", {}), ("validated", {"passed": False}), ("replan", {}),
    ("plan", {"subgoals": [{"goal": "Open orders", "irreversible": False}, {"goal": "Place the order", "irreversible": True}]}),
    ("validated", {"passed": True}), ("approval", {}),
]


class Scripted:
    def __init__(self, plan): self.plan, self.calls = plan, []

    async def json(self, *, system, user, schema, **kw):
        self.calls.append(user)
        return self.plan


def test_recorded_collects_last_plan_and_decisions():
    r = replay.recorded(EVENTS)
    assert r.goal == "reorder my last order" and len(r.plan) == 2
    assert r.validations == [False, True] and r.decisions == {"retry": 1, "replan": 1, "approval": 1}


def test_diff_flags_an_irreversible_step_that_moved():
    d = replay.diff_plans([{"goal": "A"}, {"goal": "Pay", "irreversible": True}], [{"goal": "A"}, {"goal": "Pay"}, {"goal": "B"}])
    assert d["irreversible_changed"] and d["added"] == ["b"] and d["removed"] == []


async def test_replay_compares_new_plan_with_recorded_one():
    new = Plan(subgoals=[Subgoal(id=1, goal="Open orders", success_criterion="x"),
                         Subgoal(id=2, goal="Review cart", success_criterion="y"),
                         Subgoal(id=3, goal="Place the order", success_criterion="z", irreversible=True)])
    out = await replay.replay_planning(EVENTS, Scripted(new))
    assert out["replayable"] and out["diff"]["added"] == ["open orders. then review cart."] and not out["diff"]["irreversible_changed"]
    assert "not replayed" in out["note"]


async def test_log_without_a_plan_is_not_replayable():
    out = await replay.replay_planning([("intent", {"goal": "x", "sites": ["a.com"]})], Scripted(None))
    assert out["replayable"] is False
