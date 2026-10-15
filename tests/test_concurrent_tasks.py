"""One person can run several tasks at once: each task gets its own browser, only the first holds the saved profile, and a cap keeps the
small server safe. Written, not run (test pause). Driven with stand-ins for the orchestrator's state."""

from types import SimpleNamespace

import app.agent.graph as graph
from app.agent.graph import Orchestrator
from app.browser.provider import BrowserProvider, BrowserSession


class Provider(BrowserProvider):
    def __init__(self):
        super().__init__()
        self.n, self.flags = 0, []

    async def _open(self, user_id, *, headless, scratch=False):
        self.n += 1
        self.flags.append(scratch)
        return BrowserSession(f"s{self.n}", user_id, f"ws://{self.n}")

    async def _close(self, session):
        pass


class Stub:
    def __init__(self):
        self.provider, self._live, self._headless, self._touched, self.closed = Provider(), {}, {}, {}, []
        self._resume_url = {}
        self.sessions = SimpleNamespace(restore=self._restore)
        self._access = lambda s: SimpleNamespace(allowed_domains=[])

    async def _restore(self, *a):
        return False

    async def _close_browser(self, tid):
        self.closed.append(tid)


async def test_a_users_second_task_gets_its_own_browser_and_the_first_is_left_open():
    o = Stub()
    a = await Orchestrator._ensure_browser(o, {"task_id": "t1", "user_id": "u"}, headless=True)
    b = await Orchestrator._ensure_browser(o, {"task_id": "t2", "user_id": "u"}, headless=True)
    assert a.session_id != b.session_id and o.closed == []
    assert o.provider.flags == [False, True]            # only the first has the profile


async def test_start_refuses_past_the_per_user_cap_without_touching_the_database():
    o = SimpleNamespace(s=SimpleNamespace(max_tasks_per_user=2), _driving={"a": "u", "b": "u", "c": "other"},
                        svc=SimpleNamespace(authz=SimpleNamespace(
                            principal=lambda *a: _co(None),
                            check=lambda *a: _co(SimpleNamespace(allow=True, reason="")))))
    out = await Orchestrator.start(o, user_id="u", org_id="o", thread_id="t", text="x", history="", emit=None)
    assert out.final["kind"] == "blocked" and "2 tasks running" in out.final["message"]


async def _co(v):
    return v
