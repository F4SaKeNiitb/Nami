"""Reversible steps right before the irreversible one become one subgoal; the send, and any sign-in, stay separate."""

import pytest

from app.agent.planner import ensure_sign_in_first, make_plan, merge_pre_send
from app.agent.schemas import AccessPlan, CredentialUse, Intent, Plan, Subgoal


def sg(i, goal, criterion=None, **kw):
    return Subgoal(id=i, goal=goal, success_criterion=criterion or f"{goal} done", **kw)


def test_field_by_field_steps_before_send_become_one():
    p = Plan(subgoals=[sg(1, "type the recipient"), sg(2, "type the subject"), sg(3, "type the body", "draft shows To, Subject, Body"),
                       sg(4, "send the mail", irreversible=True)])
    out = merge_pre_send(p)
    assert [g.id for g in out.subgoals] == [1, 2]
    assert out.subgoals[1].irreversible and out.subgoals[1].goal == "send the mail"
    assert out.subgoals[0].success_criterion == "draft shows To, Subject, Body"      # the read-back survives
    assert "recipient" in out.subgoals[0].goal and "subject" in out.subgoals[0].goal


def test_the_irreversible_step_is_never_merged_in():
    p = Plan(subgoals=[sg(1, "a"), sg(2, "b"), sg(3, "pay", irreversible=True), sg(4, "read receipt")])
    out = merge_pre_send(p)
    assert [g.irreversible for g in out.subgoals] == [False, True, False]


def test_a_sign_in_step_is_never_merged_across():
    p = Plan(subgoals=[sg(1, "sign in", is_auth=True), sg(2, "fill the form"), sg(3, "send", irreversible=True)])
    assert merge_pre_send(p) is p                                                        # only one reversible step: nothing to merge
    p = Plan(subgoals=[sg(1, "x"), sg(2, "sign in", is_auth=True), sg(3, "y"), sg(4, "z"), sg(5, "send", irreversible=True)])
    out = merge_pre_send(p)
    assert out.subgoals[1].is_auth
    assert out.subgoals[0].goal.startswith("x") and "y" in out.subgoals[2].goal and "z" in out.subgoals[2].goal
    assert out.subgoals[-1].irreversible and len(out.subgoals) == 4


def test_no_irreversible_step_or_a_single_step_is_left_alone():
    p = Plan(subgoals=[sg(1, "a"), sg(2, "b")])
    assert merge_pre_send(p) is p
    p = Plan(subgoals=[sg(1, "a"), sg(2, "send", irreversible=True)])
    assert merge_pre_send(p) is p


@pytest.mark.asyncio
async def test_make_plan_applies_it_and_keeps_ids_sequential():
    class LLM:
        async def json(self, **kw):
            return Plan(subgoals=[sg(7, "a"), sg(8, "b"), sg(9, "send", irreversible=True)])
    out = await make_plan(LLM(), Intent(goal="g", sites=["a.com"]), AccessPlan(allowed_domains=["a.com"]), rules_block="")
    assert [g.id for g in out.subgoals] == [1, 2] and out.subgoals[1].irreversible


def test_signed_in_replan_does_not_get_a_second_sign_in():
    access = AccessPlan(allowed_domains=["a.com"], login="password",
                        credentials=[CredentialUse(credential_id="c1", kind="password", label="pw", site="a.com")])
    p = Plan(subgoals=[sg(1, "open orders")])
    assert ensure_sign_in_first(p, access).subgoals[0].is_auth
    assert not ensure_sign_in_first(p, access, signed_in=True).subgoals[0].is_auth


@pytest.mark.asyncio
async def test_make_plan_tells_the_model_not_to_plan_a_sign_in_after_one_succeeded():
    seen = {}

    class LLM:
        async def json(self, *, system, user, **kw):
            seen["user"] = user
            return Plan(subgoals=[sg(1, "open orders")])
    access = AccessPlan(allowed_domains=["a.com"], login="password",
                        credentials=[CredentialUse(credential_id="c1", kind="password", label="pw", site="a.com")])
    out = await make_plan(LLM(), Intent(goal="g", sites=["a.com"]), access, rules_block="", signed_in=True)
    assert "Do not plan a sign-in step" in seen["user"] and not out.subgoals[0].is_auth


def test_the_merged_criterion_is_the_final_state_not_every_page_on_the_way():
    """Found live: 'orders listed; and cart shown; and checkout page' can never hold on the checkout page, so the step always failed."""
    p = Plan(subgoals=[sg(1, "open the last order", "the last order is listed"), sg(2, "reorder", "the cart shows the items"),
                       sg(3, "go to checkout", "the checkout page is shown"), sg(4, "pay", irreversible=True)])
    out = merge_pre_send(p)
    assert out.subgoals[0].success_criterion == "the checkout page is shown"
    assert "last order is listed" not in out.subgoals[0].success_criterion and "cart shows" not in out.subgoals[0].success_criterion


def test_the_planner_is_told_not_to_demand_saved_card_details_before_paying():
    """Found live: the review step required 'saved Visa card details' on a page that never shows them, so it could not pass."""
    from app.agent.planner import SYSTEM
    assert "never require a saved card" in SYSTEM and "paying step itself" in SYSTEM
