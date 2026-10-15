"""'Sign in with Google' on another site: the saved google.com login is used, scoped to Google's own page, only when the
user asked for it, and only from the user's words (found live: Canva asked for a Canva password the user does not have)."""

import pytest

from app.agent.access_planner import build_access_plan, wants_google_signin
from app.agent.planner import ensure_sign_in_first, sign_in_subgoal
from app.agent.schemas import Intent, Plan, Subgoal
from datetime import datetime

from app.rules.engine import EffectiveRules
from app.vault.service import CredentialView


def cred(id_, kind, origins, **meta):
    return CredentialView(id_, kind, f"{kind} {id_}", origins, meta, datetime(2026, 1, 1), None, False)


def rules(**policies):
    base = {"require_approval": ["purchase"], "blocked_actions": [], "allow_card_use": True, "allow_stored_passwords": True}
    return EffectiveRules(org_id="o", user_id="u", site=None, version="v1", policies={**base, **policies})


GOOGLE = cred("g1", "password", ["google.com"])
CANVA_PW = cred("c1", "password", ["canva.com"])


def sso(goal="login to canva using sign in with google and list my files", **kw):
    return Intent(goal=goal, sites=["canva.com"], needs_login=True, **kw)


@pytest.mark.parametrize("goal,yes", [
    ("login to canva using sign in with google", True), ("sign in to canva with Google", True),
    ("continue with google on figma", True), ("log in through my google account", True), ("use Google sign-in", True),
    ("sign in to canva", False), ("open my google drive", False), ("search google for canva", False)])
def test_only_an_explicit_google_sign_in_request_counts(goal, yes):
    assert wants_google_signin(Intent(goal=goal, sites=["canva.com"])) is yes
    assert wants_google_signin(Intent(goal="x", sites=["canva.com"], google_signin=True)) is True


def test_the_saved_google_login_is_used_and_google_joins_the_allowed_sites_after_the_main_one():
    p = build_access_plan(sso(), [GOOGLE], rules(), session_sites=set())
    assert p.login == "password" and p.sign_in_via == "google"
    assert p.allowed_domains == ["canva.com", "google.com"]            # the task's site stays first
    assert [c.credential_id for c in p.credentials] == ["g1"] and p.credentials[0].site == "google.com"


def test_a_site_login_does_not_stand_in_when_google_was_asked_for_and_a_google_login_exists():
    p = build_access_plan(sso(), [GOOGLE, CANVA_PW], rules(), session_sites=set())
    assert p.sign_in_via == "google" and [c.credential_id for c in p.credentials] == ["g1"]


def test_without_a_google_request_a_google_login_is_never_released_to_another_site():
    p = build_access_plan(Intent(goal="sign in to canva", sites=["canva.com"], needs_login=True), [GOOGLE], rules(), session_sites=set())
    assert p.login == "need_credentials" and p.sign_in_via is None and p.allowed_domains == ["canva.com"]


def test_a_saved_session_still_wins():
    p = build_access_plan(sso(), [GOOGLE], rules(), session_sites={"canva.com"})
    assert p.login == "session" and p.sign_in_via is None and p.allowed_domains == ["canva.com"]


def test_no_saved_google_login_says_which_one_is_missing_instead_of_asking_for_a_canva_password():
    p = build_access_plan(sso(), [], rules(), session_sites=set())
    assert p.login == "need_credentials" and p.ask.kinds == [] and "google.com" in p.ask.reason and p.ask.allow_takeover
    assert p.ask.site == "canva.com"


def test_rules_that_forbid_saved_passwords_or_google_stop_it():
    assert build_access_plan(sso(), [GOOGLE], rules(allow_stored_passwords=False), session_sites=set()).login == "need_credentials"
    p = build_access_plan(sso(), [GOOGLE], rules(domain_allow=["canva.com"]), session_sites=set())      # google.com not on the user's list
    assert p.sign_in_via is None and p.login == "need_credentials" and p.allowed_domains == ["canva.com"]


def test_a_google_login_saved_for_another_site_is_never_used():
    p = build_access_plan(sso(), [cred("x", "password", ["evil.com"])], rules(), session_sites=set())
    assert p.sign_in_via is None and p.login == "need_credentials"


def test_the_sign_in_step_says_continue_with_google_and_is_first():
    p = build_access_plan(sso(), [GOOGLE], rules(), session_sites=set())
    plan = ensure_sign_in_first(Plan(subgoals=[Subgoal(id=1, goal="list files", success_criterion="files shown")]), p)
    first = plan.subgoals[0]
    assert first.is_auth and "with Google" in first.goal and "canva.com" in first.success_criterion
    assert "with Google" not in sign_in_subgoal("canva.com", 1).goal            # the normal step is unchanged
