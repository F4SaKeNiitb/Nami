"""Browsers nobody is using are closed after 10 minutes, never while a step is running, and a resume comes back to the same page.
Written, not run (test pause). The reaper is driven with a stand-in for the orchestrator's state."""

import asyncio
from types import SimpleNamespace

import pytest

import app.agent.graph as graph
from app.agent.graph import Orchestrator
from app.core.cleanup import idle_browser_loop
from app.core.config import Settings


class Fake:
    """Just the parts of the orchestrator the reaper and the step wrapper touch."""

    def __init__(self, idle=600):
        self.s = SimpleNamespace(browser_idle_seconds=idle)
        self._live, self._busy, self._touched, self._resume_url, self.closed = {}, set(), {}, {}, []

    async def _close_browser(self, tid):
        self.closed.append(tid)
        self._live.pop(tid, None)

    def add(self, tid, age):
        self._live[tid] = SimpleNamespace(cdp_url=f"ws://{tid}")
        self._touched[tid] = graph.time.monotonic() - age


@pytest.fixture(autouse=True)
def page_url(monkeypatch):
    async def observe(cdp_url, hint="", **kw):
        return f"https://shop.example/{cdp_url[-2:]}/cart", "", None
    monkeypatch.setattr(graph, "observe", observe)


async def test_a_browser_idle_for_ten_minutes_is_closed_and_a_fresh_one_is_not():
    f = Fake()
    f.add("old", 601)
    f.add("fresh", 30)
    assert await Orchestrator.reap_idle(f) == 1
    assert f.closed == ["old"] and list(f._live) == ["fresh"]


async def test_a_browser_with_a_step_in_flight_is_never_closed_however_old():
    f = Fake()
    f.add("working", 5000)
    f._busy.add("working")
    assert await Orchestrator.reap_idle(f) == 0 and f.closed == []


async def test_the_page_it_was_on_is_remembered_so_a_resume_can_return_to_it():
    f = Fake()
    f.add("t1", 700)
    await Orchestrator.reap_idle(f)
    assert f._resume_url["t1"] == "https://shop.example/t1/cart"


async def test_an_unreachable_browser_is_still_closed_and_nothing_is_remembered(monkeypatch):
    async def dead(*a, **k):
        raise RuntimeError("gone")
    monkeypatch.setattr(graph, "observe", dead)
    f = Fake()
    f.add("t1", 700)
    assert await Orchestrator.reap_idle(f) == 1 and "t1" not in f._resume_url


async def test_a_page_that_is_not_http_is_not_remembered(monkeypatch):
    async def blank(*a, **k):
        return "about:blank", "", None
    monkeypatch.setattr(graph, "observe", blank)
    f = Fake()
    f.add("t1", 700)
    await Orchestrator.reap_idle(f)
    assert "t1" not in f._resume_url


async def test_the_limit_comes_from_settings_and_can_be_overridden():
    f = Fake(idle=60)
    f.add("a", 61)
    f.add("b", 30)
    assert await Orchestrator.reap_idle(f) == 1
    f.add("c", 100)
    assert await Orchestrator.reap_idle(f, idle_seconds=1000) == 0


async def test_one_failing_close_does_not_stop_the_others():
    f = Fake()
    f.add("a", 700)
    f.add("b", 700)
    real = f._close_browser

    async def flaky(tid):
        if tid == "a":
            raise RuntimeError("provider down")
        await real(tid)
    f._close_browser = flaky
    assert await Orchestrator.reap_idle(f) == 1 and f.closed == ["b"]


def test_default_idle_limit_is_ten_minutes():
    assert Settings().browser_idle_seconds == 600


# ───────────── the idle clock: running a step keeps the browser busy, finishing (or pausing, or failing) starts the clock ─────────────

async def test_a_step_marks_the_task_busy_and_starts_the_idle_clock_when_it_ends():
    f = Fake()
    seen = {}

    async def step(s):
        seen["busy_inside"] = "t1" in f._busy
        return {"route": "run"}
    wrapped = Orchestrator._traced(f, "run", step)
    await wrapped({"task_id": "t1", "user_id": "u", "org_id": "o"})
    assert seen["busy_inside"] and "t1" not in f._busy and "t1" in f._touched


async def test_a_step_that_pauses_or_fails_still_clears_busy():
    f = Fake()

    async def boom(s):
        raise RuntimeError("pause or crash")
    with pytest.raises(RuntimeError):
        await Orchestrator._traced(f, "run", boom)({"task_id": "t1", "user_id": "u", "org_id": "o"})
    assert "t1" not in f._busy and "t1" in f._touched


# ───────────── the background sweep ─────────────

async def test_the_sweep_runs_the_reaper_repeatedly_and_stops_on_request():
    calls, stop = [], asyncio.Event()

    class Orch:
        async def reap_idle(self):
            calls.append(1)
            if len(calls) == 3:
                stop.set()
    await asyncio.wait_for(idle_browser_loop(Orch(), stop=stop, interval_s=0.01), 2)
    assert len(calls) == 3


async def test_a_failing_sweep_is_logged_not_raised():
    stop, n = asyncio.Event(), []

    class Orch:
        async def reap_idle(self):
            n.append(1)
            stop.set()
            raise RuntimeError("boom")
    await asyncio.wait_for(idle_browser_loop(Orch(), stop=stop, interval_s=0.01), 2)
    assert n == [1]
