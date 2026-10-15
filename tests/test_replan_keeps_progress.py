"""A replan continues from what is done instead of restarting; a rejected saved login asks for a new one; suggestions are fixed text."""

import pytest

from app.agent.graph import Orchestrator
from app.agent.schemas import AccessPlan, CredentialUse, Intent, Plan, Subgoal
from app.agent.suggest import next_steps, suggestion_text


def sg(i, goal, **kw):
    return Subgoal(id=i, goal=goal, success_criterion="c", **kw)


class _Rules:
    def prompt_block(self): return ""


class _Svc:
    class rules:
        @staticmethod
        async def resolve(**kw): return _Rules()


def orch(llm_plan, monkeypatch):
    o = object.__new__(Orchestrator)
    o.svc = _Svc()
    o.events, o.asked = [], []

    async def event(s, t, p): o.events.append((t, p))

    async def ask(s, access, **kw):
        o.asked.append(kw)
        return {"route": "ask", "pending": {"type": "need_credentials"}}
    o._event, o._ask_in_chat = event, ask

    class V:
        async def list(self, uid): return []
    o.vault = V()

    class S:
        model_main = "m"
    o.s = S()
    o.llm = object()
    import app.agent.graph as g
    monkeypatch.setattr(g, "make_plan", llm_plan)
    return o


def state(idx, access, plan, **kw):
    return {"task_id": "t", "user_id": "u", "org_id": "o", "idx": idx, "attempts": 5, "replans": 0, "reflexion": "",
            "intent": Intent(goal="g", sites=["a.com"]).model_dump(), "access": access.model_dump(), "plan": plan.model_dump(),
            "evidence": ["step 1 ok"], "claims": ["claim 1"], "approved": [2], **kw}


@pytest.mark.asyncio
async def test_replan_keeps_evidence_and_claims_and_clears_approvals(monkeypatch):
    seen = {}

    async def fake_plan(llm, intent, access, **kw):
        seen.update(kw)
        return Plan(subgoals=[sg(1, "finish")])
    o = orch(fake_plan, monkeypatch)
    access = AccessPlan(allowed_domains=["a.com"], login="session")
    s = state(1, access, Plan(subgoals=[sg(1, "open orders"), sg(2, "read order")]))
    out = await o._failed(s, "the page did not show X", {"healthy": True})
    assert out["route"] == "gate" and out["idx"] == 0 and out["replans"] == 1
    assert "evidence" not in out and "claims" not in out                  # untouched: progress is kept
    assert out["approved"] == []
    assert seen["signed_in"] is True and "open orders" in seen["guidance"] and "do NOT repeat" in seen["guidance"]


@pytest.mark.asyncio
async def test_replan_after_a_successful_sign_in_is_marked_signed_in(monkeypatch):
    seen = {}

    async def fake_plan(llm, intent, access, **kw):
        seen.update(kw)
        return Plan(subgoals=[sg(1, "finish")])
    o = orch(fake_plan, monkeypatch)
    access = AccessPlan(allowed_domains=["a.com"], login="password",
                        credentials=[CredentialUse(credential_id="c1", kind="password", label="pw", site="a.com")])
    s = state(2, access, Plan(subgoals=[sg(1, "sign in", is_auth=True), sg(2, "open"), sg(3, "read")]), replans=0)
    await o._failed(s, "stuck", {})
    assert seen["signed_in"] is True


def _signin_access():
    return AccessPlan(allowed_domains=["a.com"], login="password",
                      credentials=[CredentialUse(credential_id="c1", kind="password", label="pw", site="a.com")])


@pytest.mark.asyncio
async def test_a_first_sign_in_failure_that_is_only_a_timeout_or_a_page_not_showing_is_retried_not_called_a_rejection(monkeypatch):
    """Found live: 4 of 5 'your login was rejected' messages arrived while the agent was still working."""
    async def never(*a, **k): raise AssertionError("must not replan yet")
    o = orch(never, monkeypatch)
    for reason in ("the page did not show: signed in", "this step took longer than 360 seconds and was stopped"):
        s = state(0, _signin_access(), Plan(subgoals=[sg(1, "sign in", is_auth=True), sg(2, "read")]), attempts=0)
        out = await o._failed(s, reason, {})
        assert out["route"] == "run" and out["attempts"] == 1 and not o.asked
    assert ("login_rejected", {"site": "a.com"}) not in o.events


@pytest.mark.asyncio
async def test_a_saved_login_the_site_actually_refused_asks_for_a_new_one_after_one_try_without_replanning(monkeypatch):
    async def never(*a, **k): raise AssertionError("must not replan")
    o = orch(never, monkeypatch)
    s = state(0, _signin_access(), Plan(subgoals=[sg(1, "sign in", is_auth=True), sg(2, "read")]), attempts=1)
    out = await o._failed(s, "the site said the password is incorrect", {})
    assert out["route"] == "ask" and o.asked[0]["purpose"] == "reauth"
    assert "did not accept the saved login" in o.asked[0]["reason"]
    assert ("login_rejected", {"site": "a.com"}) in o.events


@pytest.mark.asyncio
async def test_a_timeout_never_counts_as_a_rejected_login_however_many_tries(monkeypatch):
    async def fake_plan(llm, intent, access, **kw): return Plan(subgoals=[sg(1, "finish")])
    o = orch(fake_plan, monkeypatch)
    s = state(0, _signin_access(), Plan(subgoals=[sg(1, "sign in", is_auth=True), sg(2, "read")]), attempts=2)
    await o._failed(s, "this step took longer than 360 seconds and was stopped", {})
    assert ("login_rejected", {"site": "a.com"}) not in o.events and not o.asked


def test_suggestions_are_fixed_text_from_the_registry():
    assert "Drive" in suggestion_text("google.com")
    steps = next_steps("openrouter.ai")
    assert steps and steps[-1].startswith("do another task on openrouter.ai")
    assert next_steps("") == [] and suggestion_text("") == ""
    assert len(next_steps("google.com", limit=2)) == 2


# ───────────── a refused login is told apart from "the checker could not confirm it" (written, not run) ─────────────

def test_the_site_check_has_three_answers_signed_in_still_asks_for_a_login_and_could_not_tell():
    import inspect
    src = inspect.getsource(Orchestrator._site_says_signed_in)
    assert "bool | None" in src and src.count("return None") == 2
    run_src = inspect.getsource(Orchestrator.n_run)
    assert "site_says is False" in run_src and "the sign-in did not complete" in run_src


@pytest.mark.asyncio
async def test_a_site_that_still_asks_for_a_login_after_one_retry_gets_the_update_your_login_prompt(monkeypatch):
    async def never(*a, **k): raise AssertionError("must not replan")
    o = orch(never, monkeypatch)
    s = state(0, _signin_access(), Plan(subgoals=[sg(1, "sign in", is_auth=True), sg(2, "read")]), attempts=1)
    out = await o._failed(s, "the sign-in did not complete: the site's own account page still asks for a login", {})
    assert out["route"] == "ask" and "did not accept the saved login" in o.asked[0]["reason"]


@pytest.mark.asyncio
async def test_the_first_such_failure_is_retried_once_before_blaming_the_login(monkeypatch):
    async def never(*a, **k): raise AssertionError("must not replan yet")
    o = orch(never, monkeypatch)
    s = state(0, _signin_access(), Plan(subgoals=[sg(1, "sign in", is_auth=True), sg(2, "read")]), attempts=0)
    out = await o._failed(s, "the sign-in did not complete: the site's own account page still asks for a login", {})
    assert out["route"] == "run" and out["attempts"] == 1 and not o.asked
