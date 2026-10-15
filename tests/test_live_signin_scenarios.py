"""Pure scenario tests for the local real-LLM/headless-browser sign-in lab."""

import pyotp

from app.agent.schemas import Intent, Plan, Subgoal
from app.eval.fakesite import (FoodCoState, _names, accepts_password, accepts_second_factor,
                               challenge_markup, requires_human_login, second_factor_markup)
from app.browser.broker import BrokerPayload
from app.eval.live_signin_support import LocalScenarioBroker, LocalScenarioLLM, LocalScenarioPayload
from app.eval.fakesite import MODES


def test_expired_totp_is_rejected_once_then_a_fresh_totp_is_accepted():
    state = FoodCoState(mode="otp_expired")
    current = pyotp.TOTP(state.totp_seed).now()

    assert not accepts_second_factor(state, current)
    assert state.otp_expired_once
    assert accepts_second_factor(state, pyotp.TOTP(state.totp_seed).now())


def test_sms_code_accepts_only_the_simulated_user_code():
    state = FoodCoState(mode="sms")

    assert not accepts_second_factor(state, pyotp.TOTP(state.totp_seed).now())
    assert not accepts_second_factor(state, "000000")
    assert accepts_second_factor(state, "639214")


def test_rejected_otp_page_keeps_the_code_input_for_a_fresh_retry():
    markup = second_factor_markup("otp_expired", "That code is not right.")

    assert "That code is not right." in markup
    assert 'name="code"' in markup
    assert 'id="verify"' in markup


def test_human_only_challenges_are_visible_and_do_not_remove_login_form():
    assert "CAPTCHA" in challenge_markup("captcha")
    assert "passkey" in challenge_markup("passkey")
    assert challenge_markup("none") == ""
    assert requires_human_login("captcha") and requires_human_login("passkey")
    assert not requires_human_login("none")


def test_layout_drift_changes_form_and_action_selectors():
    normal, renamed = _names("none"), _names("renamed")

    assert normal["user"] != renamed["user"]
    assert normal["otp"] != renamed["otp"]
    assert normal["login_btn"] != renamed["login_btn"]


def test_bad_password_is_a_dedicated_scenario():
    state = FoodCoState(mode="bad_password")
    assert not accepts_password(state, state.username, state.password)


def test_normal_password_login_requires_both_expected_fields():
    state = FoodCoState()
    assert accepts_password(state, state.username, state.password)
    assert not accepts_password(state, state.username, "wrong")


async def test_harness_pins_fake_host_and_read_only_scope_after_real_intent_call():
    class Delegate:
        usage = {"calls": 1}

        async def json(self, **kwargs):
            assert kwargs["schema"] is Intent
            return Intent(goal="show the order", sites=[], needs_login=False, actions=["purchase"])

    llm = LocalScenarioLLM(Delegate(), "127.0.0.1", "http://127.0.0.1:12345")
    intent = await llm.json(schema=Intent)

    assert intent.sites == ["127.0.0.1"] and intent.needs_login
    assert intent.actions == [] and not intent.generate and intent.unsupported is None
    assert llm.usage == {"calls": 1}


async def test_harness_can_preserve_purchase_intent_for_approval_gate_scenario():
    class Delegate:
        usage = {}

        async def json(self, **kwargs):
            return Intent(goal="reorder", sites=[], needs_login=False, actions=[])

    llm = LocalScenarioLLM(Delegate(), "127.0.0.1", "http://127.0.0.1:12345", preserve_purchase_action=True)
    intent = await llm.json(schema=Intent)

    assert intent.actions == ["purchase"]


async def test_harness_keeps_the_fake_sites_dynamic_port_in_the_signin_step():
    class Delegate:
        usage = {}

        async def json(self, **kwargs):
            assert kwargs["schema"] is Plan
            return Plan(subgoals=[Subgoal(id=1, goal="Sign in with the saved login", success_criterion="account page", is_auth=True)])

    llm = LocalScenarioLLM(Delegate(), "127.0.0.1", "http://127.0.0.1:12345")
    plan = await llm.json(schema=Plan)

    assert plan.subgoals[0].is_auth
    assert "http://127.0.0.1:12345" in plan.subgoals[0].goal


def test_fake_local_http_origin_gets_placeholders_without_enabling_http_for_real_sites():
    class Vault:  # Broker.build is not called; initialize only the payload transformation inputs.
        pass

    broker = LocalScenarioBroker(Vault(), user_id="u", task_id="t", site="127.0.0.1")
    payload = BrokerPayload(sensitive_data={"https://*.127.0.0.1": {"cred_user": "synthetic"}})

    broker._allow_local_http(payload)

    assert payload.sensitive_data == {"http*://*.127.0.0.1": {"cred_user": "synthetic"}}


def test_user_supplied_code_is_scoped_to_the_local_http_origin():
    payload = LocalScenarioPayload(BrokerPayload(), site="127.0.0.1")

    payload.add_one_time("127.0.0.1", "otp_code", "639214")

    assert payload.sensitive_data == {"http*://*.127.0.0.1": {"otp_code": "639214"}}
    assert payload.redact("code 639214") == "code [redacted]"



def test_user_supplied_code_for_non_local_site_keeps_https_scope():
    payload = LocalScenarioPayload(BrokerPayload(), site="127.0.0.1")

    payload.add_one_time("shop.example", "otp_code", "817204")

    assert payload.sensitive_data == {"https://*.shop.example": {"otp_code": "817204"}}


def test_local_http_exception_does_not_change_external_site_credentials():
    broker = LocalScenarioBroker(object(), user_id="u", task_id="t", site="127.0.0.1")
    payload = BrokerPayload(sensitive_data={"https://*.shop.example": {"cred_user": "synthetic"}})

    broker._allow_local_http(payload)

    assert payload.sensitive_data == {"https://*.shop.example": {"cred_user": "synthetic"}}


def test_secret_values_are_redacted_and_only_placeholders_appear_in_hints():
    payload = BrokerPayload(sensitive_data={"https://*.shop.example": {"cred_user": "person@example.test"}},
                            placeholders=["cred_user"], _values=["person@example.test"])

    hint = payload.prompt_hint()

    assert "<secret>cred_user</secret>" in hint
    assert "person@example.test" not in hint
    assert payload.redact("typed person@example.test") == "typed [redacted]"


def test_username_only_login_hint_directs_the_agent_to_user_provided_code():
    payload = BrokerPayload(placeholders=["cred_foodco_username"], _values=["me@foodco.test"])

    hint = payload.prompt_hint()

    assert "No password is saved" in hint
    assert "request_help(kind='code')" in hint
    assert "me@foodco.test" not in hint


def test_reset_run_clears_auth_attempts_and_side_effects():
    state = FoodCoState(mode="otp_expired", sessions={"sid": 3}, orders_placed=2,
                        last_order={"id": 5002}, cart=["item"], log=["/orders"], bodies=["/login"],
                        expired_once=True, otp_expired_once=True, orders_viewed=4,
                        login_attempts=3, second_factor_attempts=2, login_page_failures=1,
                        sms_rejected_once=True, expired_sessions=2)

    state.reset_run()

    assert not state.sessions and state.orders_placed == 0 and state.last_order is None
    assert not state.cart and not state.log and not state.bodies
    assert not state.expired_once and not state.otp_expired_once
    assert state.orders_viewed == state.login_attempts == state.second_factor_attempts == state.login_page_failures == 0
    assert not state.sms_rejected_once and state.expired_sessions == 0


def test_assignment_scenarios_are_in_the_default_live_matrix():
    assert {"none", "renamed", "moved", "modal", "slow", "expiry", "ab", "inject", "sms", "otp_expired",
            "bad_password", "captcha", "passkey", "passwordless", "sms_rejected", "missing_credentials",
            "empty_orders", "sso_required", "rate_limited", "account_locked", "purchase_guard", "purchase_approved",
            "purchase_cancelled", "reuse_session", "credential_resume", "credential_cancelled", "email_reveal",
            "transient_error", "bad_username", "sms_retry", "expiry_loop"} <= set(MODES)


def test_passwordless_and_rejected_sms_have_distinct_code_policies():
    assert accepts_second_factor(FoodCoState(mode="passwordless"), "639214")
    assert not accepts_second_factor(FoodCoState(mode="sms_rejected"), "639214")


def test_assignment_includes_empty_access_and_sso_cases():
    assert {"empty_orders", "missing_credentials", "sso_required"} <= set(MODES)


def test_assignment_includes_throttling_lockout_and_approval_safety_cases():
    assert {"rate_limited", "account_locked", "purchase_guard", "purchase_approved", "purchase_cancelled"} <= set(MODES)


def test_assignment_includes_session_reuse_as_a_second_live_task():
    assert "reuse_session" in MODES


def test_assignment_includes_secure_credential_resume_and_progressive_login():
    assert {"credential_resume", "credential_cancelled", "email_reveal", "transient_error"} <= set(MODES)


def test_progressive_email_login_uses_the_shifted_field_names():
    names = _names("email_reveal")

    assert names["user"] == "u_f1" and names["pw"] == "p_f9"


def test_sms_retry_rejects_once_then_accepts_only_the_correct_code():
    state = FoodCoState(mode="sms_retry")

    assert not accepts_second_factor(state, "000000")
    assert state.sms_rejected_once
    assert accepts_second_factor(state, "639214")
    assert not accepts_second_factor(state, "123456")


def test_repeated_expiry_and_bad_username_have_separate_scenarios():
    assert {"expiry_loop", "bad_username"} <= set(MODES)
