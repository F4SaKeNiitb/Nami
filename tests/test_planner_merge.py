"""Simple read-only lookups become ONE subgoal; anything with a sign-in, money or approval keeps its checkpoints."""

import pytest

from app.agent.planner import MERGE_MAX_SUBGOALS, ensure_sign_in_first, make_plan, merge_read_only
from app.agent.schemas import AccessPlan, CredentialUse, Intent, Plan, Subgoal


def sg(i, goal, criterion=None, **kw):
    return Subgoal(id=i, goal=goal, success_criterion=criterion or f"{goal} done", **kw)


def plan(*sgs):
    return Plan(subgoals=list(sgs))


PUBLIC = AccessPlan(allowed_domains=["a.com"])


# ───────────── must NOT merge (safety first) ─────────────

@pytest.mark.parametrize("access", [
    AccessPlan(allowed_domains=["a.com"], login="password", credentials=[CredentialUse(credential_id="c1", kind="password", label="pw", site="a.com")]),
    AccessPlan(allowed_domains=["a.com"], login="session"),
    AccessPlan(allowed_domains=["a.com"], login="need_credentials", connect_site="a.com"),
    AccessPlan(allowed_domains=["a.com"], approval_actions=["purchase"]),
    AccessPlan(allowed_domains=["a.com"], login="api"),
], ids=["password", "session", "need-credentials", "approval-actions", "api"])
def test_tasks_with_a_login_credential_or_approval_are_never_merged(access):
    p = plan(sg(1, "open"), sg(2, "read"))
    assert merge_read_only(p, access) is p


def test_an_irreversible_step_is_never_merged_away():
    p = plan(sg(1, "open the cart"), sg(2, "place the order", irreversible=True))
    out = merge_read_only(p, PUBLIC)
    assert out is p and out.subgoals[1].irreversible


def test_a_sign_in_step_is_never_merged_away():
    p = plan(sg(1, "sign in", is_auth=True), sg(2, "read"))
    assert merge_read_only(p, PUBLIC) is p


def test_longer_plans_keep_their_checkpoints():
    p = plan(*[sg(i, f"step {i}") for i in range(1, MERGE_MAX_SUBGOALS + 2)])
    assert merge_read_only(p, PUBLIC) is p


def test_a_single_subgoal_is_left_alone():
    p = plan(sg(1, "read it"))
    assert merge_read_only(p, PUBLIC) is p


def test_a_card_in_the_access_plan_blocks_merging():
    acc = AccessPlan(allowed_domains=["a.com"], cards=[CredentialUse(credential_id="k", kind="card", label="card", site="a.com")])
    p = plan(sg(1, "a"), sg(2, "b"))
    assert merge_read_only(p, acc) is p


# ───────────── merges ─────────────

def test_a_public_two_step_lookup_becomes_one_subgoal_with_both_jobs():
    out = merge_read_only(plan(sg(1, "Open animeschedule.net", "today's column is visible"),
                               sg(2, "Read every anime airing today", "all titles and times are listed")), PUBLIC)
    assert len(out.subgoals) == 1
    g = out.subgoals[0]
    assert g.id == 1 and not g.irreversible and not g.is_auth
    assert "Open animeschedule.net" in g.goal and "Read every anime airing today" in g.goal
    assert g.goal.index("Open") < g.goal.index("Read every")                  # order of work is kept
    assert "today's column is visible" in g.success_criterion and "all titles and times are listed" in g.success_criterion


def test_the_maximum_size_still_merges_and_duplicate_criteria_are_collapsed():
    out = merge_read_only(plan(*[sg(i, f"do {i}", "page shows X") for i in range(1, MERGE_MAX_SUBGOALS + 1)]), PUBLIC)
    assert len(out.subgoals) == 1 and out.subgoals[0].success_criterion.count("page shows X") == 1


def test_merged_text_is_bounded():
    out = merge_read_only(plan(sg(1, "a" * 2000, "c" * 2000), sg(2, "b" * 2000, "d" * 2000)), PUBLIC)
    assert len(out.subgoals[0].goal) <= 900 and len(out.subgoals[0].success_criterion) <= 900


# ───────────── through make_plan ─────────────

class _LLM:
    def __init__(self, p):
        self.p, self.seen = p, []

    async def json(self, *, system, user, schema, images=None, model=None, max_tokens=1500):
        self.seen.append(system)
        return self.p.model_copy(deep=True)


async def test_make_plan_applies_the_merge_and_numbers_subgoals():
    llm = _LLM(plan(sg(9, "open"), sg(7, "read")))
    out = await make_plan(llm, Intent(goal="g", sites=["a.com"]), PUBLIC, rules_block="")
    assert [g.id for g in out.subgoals] == [1] and "ONE subgoal" in llm.seen[0]


async def test_make_plan_keeps_an_approval_flow_intact_and_numbered():
    acc = AccessPlan(allowed_domains=["a.com"], approval_actions=["purchase"])
    llm = _LLM(plan(sg(5, "add to cart"), sg(6, "pay", irreversible=True)))
    out = await make_plan(llm, Intent(goal="g", sites=["a.com"]), acc, rules_block="")
    assert [g.id for g in out.subgoals] == [1, 2] and out.subgoals[1].irreversible


def test_the_planner_is_told_to_name_the_answer_in_a_merged_lookup():
    from app.agent.planner import SYSTEM
    assert "ONE subgoal" in SYSTEM and "name the answer to report" in SYSTEM and "require that answer to be on the page" in SYSTEM
    assert "irreversible: true ONLY" in SYSTEM                                  # the approval rule is untouched


# ───────────── a saved login always means: sign in first ─────────────

def test_ensure_sign_in_first_inserts_a_sign_in_step_and_renumbers():
    access = AccessPlan(allowed_domains=["a.com"], login="password")
    out = ensure_sign_in_first(plan(sg(1, "open orders"), sg(2, "read them")), access)
    assert [g.id for g in out.subgoals] == [1, 2, 3] and out.subgoals[0].is_auth and out.subgoals[1].goal == "open orders"
    assert "placeholders" in out.subgoals[0].goal and "a.com" in out.subgoals[0].goal


def test_ensure_sign_in_first_leaves_other_plans_alone():
    p = plan(sg(1, "open orders"), sg(2, "read them"))
    assert ensure_sign_in_first(p, AccessPlan(allowed_domains=["a.com"], login="session")) is p
    already = plan(Subgoal(id=1, goal="sign in", success_criterion="c", is_auth=True), sg(2, "read"))
    assert ensure_sign_in_first(already, AccessPlan(allowed_domains=["a.com"], login="password")) is already


async def test_planner_forces_a_sign_in_first_subgoal_when_login_is_password():
    class LLM:
        prompt = ""

        async def json(self, *, system, user, schema, model=None, **kw):
            LLM.prompt = user
            return plan(sg(1, "read the orders"))
    access = AccessPlan(allowed_domains=["a.com"], login="password",
                        credentials=[CredentialUse(credential_id="c1", kind="password", label="pw", site="a.com")])
    out = await make_plan(LLM(), Intent(goal="read orders", sites=["a.com"], needs_login=True), access, rules_block="")
    assert out.subgoals[0].is_auth and out.subgoals[1].goal == "read the orders"
    assert "MUST be `is_auth: true`" in LLM.prompt


async def test_planner_tells_the_model_not_to_plan_a_sign_in_when_already_signed_in():
    class LLM:
        prompt = ""

        async def json(self, *, system, user, schema, model=None, **kw):
            LLM.prompt = user
            return plan(sg(1, "read the orders"))
    access = AccessPlan(allowed_domains=["a.com"], login="session")
    out = await make_plan(LLM(), Intent(goal="read orders", sites=["a.com"], needs_login=True), access, rules_block="")
    assert "Do not plan a sign-in step" in LLM.prompt and not out.subgoals[0].is_auth


def test_prompts_tell_the_model_that_an_empty_result_is_a_valid_answer():
    """Found live: 'list my Canva files' on an empty account was planned as 'a list showing filenames, thumbnails and dates',
    so the validator failed a correct 'there are none' and the task retried for minutes. The model's behaviour is checked live;
    this keeps the rule from being edited out."""
    from app.agent import planner, validator
    assert "there is nothing" in planner.SYSTEM and "clearly shows there are none" in planner.SYSTEM
    assert "empty result" in validator.SYSTEM and "An unclear, loading or error page is not an empty result" in validator.SYSTEM
