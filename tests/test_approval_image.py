"""The page picture on an approval card: owner only, only while an approval is pending, memory only, gone when the task ends."""

from types import SimpleNamespace

import pytest

from app.agent.graph import Orchestrator


def orch(*, owner=True, pending=None):
    o = Orchestrator.__new__(Orchestrator)
    o._shots, o._codes, o._live = {"t1": b"PNG"}, {}, {}
    o._asked_at, o._touched, o._resume_url, o._budget = {}, {}, {}, {}      # state added since this fixture was written

    async def owns(task_id, user_id, action):
        return owner and user_id == "u1"

    async def get_pending(task_id):
        return pending
    async def close_browser(task_id):
        return None
    o._owns, o._pending, o._close_browser = owns, get_pending, close_browser
    return o


async def test_the_owner_sees_the_picture_while_an_approval_is_pending():
    assert await orch(pending={"type": "approval"}).approval_image("t1", "u1") == b"PNG"


async def test_someone_else_gets_nothing():
    assert await orch(pending={"type": "approval"}).approval_image("t1", "u2") is None
    assert await orch(owner=False, pending={"type": "approval"}).approval_image("t1", "u1") is None


@pytest.mark.parametrize("pending", [None, {"type": "need_credentials"}, {"type": "ask_user"}, {"type": "takeover"}])
async def test_no_picture_unless_an_approval_is_what_is_pending(pending):
    assert await orch(pending=pending).approval_image("t1", "u1") is None


async def test_the_picture_is_dropped_when_the_task_is_cleaned_up():
    o = orch(pending={"type": "approval"})
    await o._cleanup("t1", capture=False)
    assert "t1" not in o._shots and await o.approval_image("t1", "u1") is None


def test_the_picture_is_never_part_of_graph_state_or_events():
    """Held in self._shots only: State has no field for it, so it cannot reach the SQLite checkpoint."""
    from app.agent.graph import State
    assert not [k for k in State.__annotations__ if "shot" in k or "image" in k or "screenshot" in k]
