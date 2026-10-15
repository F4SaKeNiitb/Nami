"""A passing sign-in check is remembered briefly per (user, site); revoke forgets it; a failure is never cached."""

import time

from app.browser.session_manager import SessionManager


def mgr(ttl=300.0):
    return SessionManager(db=None, vault=None, health_ttl=ttl)


def test_a_passing_check_is_remembered_per_user_and_site():
    m = mgr()
    m.remember_health("u1", "a.com", True)
    assert m.health_cached("u1", "a.com")
    assert not m.health_cached("u2", "a.com") and not m.health_cached("u1", "b.com")


def test_a_failed_check_is_never_cached_and_clears_an_old_pass():
    m = mgr()
    m.remember_health("u1", "a.com", True)
    m.remember_health("u1", "a.com", False)
    assert not m.health_cached("u1", "a.com")


def test_it_expires():
    m = mgr(ttl=0.01)
    m.remember_health("u1", "a.com", True)
    time.sleep(0.03)
    assert not m.health_cached("u1", "a.com")


def test_ttl_zero_means_always_check():
    m = mgr(ttl=0)
    m.remember_health("u1", "a.com", True)
    assert not m.health_cached("u1", "a.com")


async def test_revoke_forgets_it():
    import contextlib

    class DB:
        @contextlib.asynccontextmanager
        async def session(self):
            class S:
                async def scalar(self, *_): return None
            yield S()
    m = SessionManager(db=DB(), vault=None)
    m.remember_health("u1", "a.com", True)
    await m.revoke("u1", "a.com")
    assert not m.health_cached("u1", "a.com")


async def test_a_check_that_errors_lets_the_task_go_on_but_is_never_cached_as_a_pass():
    """Found live: a health check that timed out was treated as a pass and remembered, so later tasks skipped the check."""
    from app.sites.loader import SiteHints
    m = mgr()
    calls = []

    async def boom(cdp, hints):
        calls.append(1)
        raise TimeoutError("goto timed out")
    m.is_logged_in = boom
    assert await m.check("u1", "a.com", "ws://x", SiteHints(site="a.com")) is True
    assert not m.health_cached("u1", "a.com")
    await m.check("u1", "a.com", "ws://x", SiteHints(site="a.com"))
    assert len(calls) == 2                        # asked again, not served from the cache


async def test_check_remembers_a_real_pass_and_not_a_real_failure():
    from app.sites.loader import SiteHints
    m = mgr()
    results = iter([True, False])

    async def fake(cdp, hints):
        return next(results)
    m.is_logged_in = fake
    assert await m.check("u1", "a.com", "ws://x", SiteHints(site="a.com")) is True and m.health_cached("u1", "a.com")
    m._healthy.clear()
    assert await m.check("u1", "a.com", "ws://x", SiteHints(site="a.com")) is False and not m.health_cached("u1", "a.com")
