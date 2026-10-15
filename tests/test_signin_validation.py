"""The validator must be able to confirm a sign-in. Found live: with no page text at all, a real model could never pass a sign-in
step, so every saved password looked rejected and the user was asked for a new login."""

from app.agent.graph import observe_flags
from app.agent.schemas import Subgoal


def sg(**kw):
    return Subgoal(id=1, goal="g", success_criterion="c", **kw)


def test_a_sign_in_step_is_validated_on_page_text():
    assert observe_flags(sg(is_auth=True))["with_text"] is True


def test_a_sign_in_step_never_sends_a_screenshot():
    assert observe_flags(sg(is_auth=True))["with_shot"] is False


def test_other_steps_get_text_and_a_screenshot():
    assert observe_flags(sg()) == {"with_text": True, "with_shot": True}


# ───────────── the site's own signed-in page backs up an unsure validator ─────────────

from types import SimpleNamespace
from unittest import mock

import pytest

from app.agent.graph import Orchestrator
from app.agent.schemas import AccessPlan
from app.sites.loader import SiteHints


def _orch(ok=True, boom=False):
    o = Orchestrator.__new__(Orchestrator)
    check = mock.AsyncMock(side_effect=RuntimeError("x")) if boom else mock.AsyncMock(return_value=ok)
    o.sessions = SimpleNamespace(is_logged_in=check, remember_health=mock.Mock())
    return o, check


_S, _LIVE = {"user_id": "u1"}, SimpleNamespace(cdp_url="ws://x")
_ACC = AccessPlan(allowed_domains=["acme.com"], login="password")


@pytest.mark.parametrize("ok", [True, False])
async def test_the_account_page_check_decides_for_a_site_that_declares_one(ok):
    o, check = _orch(ok)
    with mock.patch("app.agent.graph.hints_for", lambda s: SiteHints(site="acme.com", auth_check_url="https://acme.com/me")):
        assert await o._site_says_signed_in(_S, _ACC, _LIVE) is ok
    check.assert_awaited_once()
    assert o.sessions.remember_health.called is ok            # a pass is remembered, a fail is not


async def test_a_site_without_an_account_page_cannot_overrule_the_validator():
    """Without an auth_check_url, the independent check cannot confirm or reject sign-in."""
    o, check = _orch(True)
    with mock.patch("app.agent.graph.hints_for", lambda s: SiteHints(site="acme.com")):
        assert await o._site_says_signed_in(_S, _ACC, _LIVE) is None
    check.assert_not_awaited()


async def test_a_browser_error_during_the_check_is_not_a_pass():
    o, _ = _orch(boom=True)
    with mock.patch("app.agent.graph.hints_for", lambda s: SiteHints(site="acme.com", auth_check_url="https://acme.com/me")):
        assert await o._site_says_signed_in(_S, _ACC, _LIVE) is None
    o.sessions.remember_health.assert_not_called()
