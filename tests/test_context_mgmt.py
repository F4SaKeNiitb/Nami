"""Bounded context for the browser agent and per-subgoal tool masking (pure code; no model calls)."""

import pytest
from browser_use import Tools

from app.agent.context import (NOTES_MAX_CHARS, asks_for_secret, compact_answers, compact_notes, is_read_only, masked_actions)
from app.agent.schemas import AccessPlan, CredentialUse, Subgoal
from app.browser.executor import EXCLUDED_ACTIONS


def sg(**kw):
    return Subgoal(id=1, goal="g", success_criterion="c", **kw)


def test_empty_and_duplicate_notes():
    assert compact_notes(None) == "" and compact_notes(["", "  "]) == ""
    assert compact_notes(["a done.", "a  done."]) == "a done."


def test_recent_notes_stay_whole_and_older_ones_shrink_to_a_sentence():
    notes = [f"Step {i} finished. " + "long detail " * 30 for i in range(6)]
    out = compact_notes(notes)
    assert out.startswith("3 earlier step(s): Step 0 finished.; Step 1 finished.; Step 2 finished.")
    assert "long detail" not in out.split(" | ")[0]            # the digest has no detail
    assert out.count(" | ") == 3 and "Step 5 finished." in out


def test_the_block_has_a_hard_cap_and_keeps_the_newest_tail():
    notes = [f"n{i} " + "x" * 400 for i in range(40)]
    out = compact_notes(notes)
    assert len(out) <= NOTES_MAX_CHARS and out.startswith("…")
    assert "n39" in out


def test_answers_keep_only_the_newest_few_and_are_bounded():
    out = compact_answers(["one", "two", "three " + "y" * 900])
    assert "one" not in out and "two" in out and len(out) <= 2 * 300 + 3
    assert compact_answers(None) == ""


@pytest.mark.parametrize("q,expected", [
    ("What's your password for Amazon?", True), ("Please type the verification code", True), ("Enter your card number", True),
    ("What is your API key?", True), ("Which size do you want, 40 or 42?", False), ("What date should I book?", False),
    ("Is the shipping pin code 560001 right?", True),        # over-blocking is the safe direction
])
def test_secret_shaped_questions_are_recognised(q, expected):
    assert asks_for_secret(q) is expected


def test_sign_in_steps_cannot_read_pages_wholesale():
    access = AccessPlan(allowed_domains=["a.com"], login="password")
    m = masked_actions(sg(is_auth=True), access)
    assert {"extract", "read_long_content", "search_page"} <= set(m)
    assert "screenshot" in EXCLUDED_ACTIONS                                              # never offered to any step, so not repeated in the mask
    assert not {"input", "click", "navigate", "send_keys", "done"} & set(m)           # sign-in keeps what it needs


def test_public_read_only_lookup_loses_only_form_widgets():
    access = AccessPlan(allowed_domains=["a.com"])
    assert is_read_only(sg(), access)
    assert set(masked_actions(sg(), access)) == {"dropdown_options", "select_dropdown"}


@pytest.mark.parametrize("access,sub", [
    (AccessPlan(allowed_domains=["a.com"], login="session"), sg()),
    (AccessPlan(allowed_domains=["a.com"], approval_actions=["send"]), sg()),
    (AccessPlan(allowed_domains=["a.com"]), sg(irreversible=True)),
    (AccessPlan(allowed_domains=["a.com"], credentials=[CredentialUse(credential_id="c", site="a.com", kind="password", label="L")]), sg()),
])
def test_anything_else_is_not_masked(access, sub):
    assert not is_read_only(sub, access)
    assert masked_actions(sub, access) == []


def test_masked_names_are_real_browser_use_actions_and_never_the_agents_exits():
    real = set(Tools().registry.registry.actions)
    every = set(masked_actions(sg(is_auth=True), AccessPlan(allowed_domains=["a.com"])) +
                masked_actions(sg(), AccessPlan(allowed_domains=["a.com"])))
    assert every <= real
    assert not every & {"done", "request_help", "ask_user", "confirm_action"}
    assert not every & set(EXCLUDED_ACTIONS)               # no overlap with the permanent exclusions
