"""Orchestrator.paused_in_thread: how a chat surface finds a card that was waiting when the server restarted.
Read only, owner only, and only a task with a saved pause (never one that crashed mid-step)."""

from types import SimpleNamespace

import pytest

from app.agent.graph import Orchestrator
from app.db.models import Task

CARD = {"type": "approval", "subgoal": "Pay", "reason": "spends money"}


@pytest.fixture
async def world(svc):
    a = await svc.tenancy.ensure_user(google_sub="ga", email="a@x.com", name="A")
    b = await svc.tenancy.ensure_user(google_sub="gb", email="b@x.com", name="B")
    saved: dict[str, dict | None] = {}

    async def _pending(task_id):
        return saved.get(task_id)
    fake = SimpleNamespace(svc=svc, _pending=_pending)

    async def add(id_, user, thread="th1", status="blocked", result=None, card=CARD):
        async with svc.db.session() as s:
            extra = {"result": result} if result is not None else {}      # a task in flight has no result row value at all
            s.add(Task(id=id_, org_id=user.default_org_id, user_id=user.id, thread_id=thread, request_text="r", status=status, **extra))
        saved[id_] = card
    return SimpleNamespace(a=a, b=b, add=add, saved=saved, ask=lambda u, th="th1": Orchestrator.paused_in_thread(fake, u.id, th))


async def test_finds_the_users_waiting_task_and_its_card(world):
    await world.add("t1", world.a)
    assert await world.ask(world.a) == ("t1", CARD)


async def test_never_returns_another_users_task_even_in_the_same_thread(world):
    await world.add("t1", world.b)
    assert await world.ask(world.a) is None


async def test_only_the_asked_conversation(world):
    await world.add("t1", world.a, thread="other-dm")
    assert await world.ask(world.a) is None


async def test_a_task_that_crashed_mid_step_has_no_saved_pause_so_is_not_returned(world):
    await world.add("t1", world.a, card=None)
    assert await world.ask(world.a) is None


@pytest.mark.parametrize("status", ["running", "done", "failed", "cancelled", "planning"])
async def test_tasks_that_are_not_waiting_are_not_returned(world, status):
    await world.add("t1", world.a, status=status)
    assert await world.ask(world.a) is None


async def test_a_finished_task_is_not_returned_even_if_its_status_is_stale(world):
    await world.add("t1", world.a, result={"kind": "result"})
    assert await world.ask(world.a) is None


async def test_the_newest_waiting_task_wins_and_older_dead_ones_are_skipped(world):
    await world.add("old", world.a, card=None)
    await world.add("new", world.a, status="awaiting_approval")
    assert (await world.ask(world.a))[0] == "new"
