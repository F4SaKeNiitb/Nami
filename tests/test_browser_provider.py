"""Provider contract: one writer per profile, close always runs. No real browser needed."""

import asyncio

import pytest

from app.browser.provider import BrowserProvider, BrowserSession, ProfileBusy, browser_session


class Fake(BrowserProvider):
    def __init__(self, fail_open=False, fail_close=False):
        super().__init__()
        self.opened, self.closed, self.fail_open, self.fail_close = 0, [], fail_open, fail_close

    async def _open(self, user_id, *, headless, scratch=False):
        self.scratch_flags = [*getattr(self, 'scratch_flags', []), scratch]
        await asyncio.sleep(0)
        if self.fail_open:
            raise RuntimeError("launch failed")
        self.opened += 1
        return BrowserSession(f"s{self.opened}", user_id, "http://x")

    async def _close(self, session):
        self.closed.append(session.session_id)
        if self.fail_close:
            raise RuntimeError("close failed")


async def test_second_open_for_same_user_is_refused():
    p = Fake()
    s = await p.open("alice")
    with pytest.raises(ProfileBusy):
        await p.open("alice")
    await p.close(s)
    await p.close(await p.open("alice")) if False else None
    assert not p.is_open("alice")
    s2 = await p.open("alice")                     # free again after close
    assert s2.session_id == "s2"


async def test_concurrent_opens_race_has_exactly_one_winner():
    p = Fake()
    results = await asyncio.gather(*[p.open("alice") for _ in range(8)], return_exceptions=True)
    assert sum(isinstance(r, BrowserSession) for r in results) == 1
    assert sum(isinstance(r, ProfileBusy) for r in results) == 7


async def test_different_users_do_not_block_each_other():
    p = Fake()
    a, b = await asyncio.gather(p.open("alice"), p.open("bob"))
    assert a.user_id == "alice" and b.user_id == "bob"


async def test_failed_launch_releases_the_lock():
    p = Fake(fail_open=True)
    with pytest.raises(RuntimeError):
        await p.open("alice")
    assert not p.is_open("alice")
    p.fail_open = False
    await p.open("alice")


async def test_context_manager_closes_when_the_task_raises():
    p = Fake()
    with pytest.raises(ValueError):
        async with browser_session(p, "alice"):
            raise ValueError("task blew up")
    assert p.closed == ["s1"] and not p.is_open("alice")


async def test_context_manager_closes_on_cancellation():
    p = Fake()

    async def task():
        async with browser_session(p, "alice"):
            await asyncio.sleep(10)

    t = asyncio.create_task(task())
    await asyncio.sleep(0.05)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert p.closed == ["s1"] and not p.is_open("alice")


async def test_lock_is_released_even_if_close_itself_fails():
    p = Fake(fail_close=True)
    s = await p.open("alice")
    with pytest.raises(RuntimeError):
        await p.close(s)
    assert not p.is_open("alice")


async def test_stale_close_does_not_release_a_newer_session():
    p = Fake()
    old = await p.open("alice")
    await p.close(old)
    new = await p.open("alice")
    await p.close(old)                              # double close of the old one
    assert p.is_open("alice")
    await p.close(new)


async def test_a_second_browser_for_the_same_user_is_a_profile_less_scratch_one_when_asked():
    p = Fake()
    first = await p.open("alice")
    second = await p.open("alice", scratch_if_busy=True)
    assert first.session_id != second.session_id
    assert p.scratch_flags == [False, True]                        # the second has no profile; the first still holds it
    await p.close(second)
    assert p.is_open("alice")                                      # closing the scratch one does not release the profile
    await p.close(first)
    assert not p.is_open("alice")
