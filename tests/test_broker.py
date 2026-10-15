"""Credential Broker: the model sees placeholders only; every decrypt is origin-bound and logged."""

import json
import logging

import pyotp
import pytest

from app.agent.schemas import AccessPlan, CredentialUse
from app.browser.broker import Broker, BrokerPayload
from app.vault.kms import LocalKms
from app.vault.service import OriginDenied, VaultError, VaultService

PW = "Tr0ub4dor&3-unique-marker"
USER = "me@x.com"
SEED = pyotp.random_base32(32)
CARD = "4111111111111111"
CVV = "737"


@pytest.fixture
async def vault(svc):
    return VaultService(svc.db, LocalKms())


@pytest.fixture
async def uid(svc):
    return (await svc.tenancy.ensure_user(google_sub="g-a", email="a@x.com", name="A")).id


async def _setup(vault, uid, *, site="swiggy.com", totp_params=None):
    pw = await vault.store(user_id=uid, kind="password", label="Swiggy", origins=[site],
                           secret={"username": USER, "password": PW})
    totp = await vault.store(user_id=uid, kind="totp", label="Swiggy 2FA", origins=[site],
                             secret={"seed": SEED if not totp_params else f"otpauth://totp/x?secret={SEED}&digits={totp_params['digits']}&period={totp_params['period']}"})
    card = await vault.store(user_id=uid, kind="card", label="Visa", origins=[site],
                             secret={"number": CARD, "cvv": CVV, "exp": "12/29"})
    cu = lambda c, k: CredentialUse(credential_id=c.id, kind=k, label=c.label, site=site)  # noqa: E731
    return AccessPlan(allowed_domains=[site], login="password",
                      credentials=[cu(pw, "password"), cu(totp, "totp")], cards=[cu(card, "card")])


async def test_values_only_in_sensitive_data_never_in_prompt_or_repr(vault, uid, caplog):
    caplog.set_level(logging.DEBUG)
    access = await _setup(vault, uid)
    p = await Broker(vault, user_id=uid, task_id="t1").build(access)
    model_visible = p.prompt_hint() + repr(p) + json.dumps(p.placeholders) + " ".join(p.notes)
    for secret in (PW, USER, SEED):
        assert secret not in model_visible
    assert PW not in caplog.text and SEED not in caplog.text
    # the browser layer does get the values, keyed by site pattern
    assert p.sensitive_data["https://*.swiggy.com"][p.placeholders[0]] in (USER, PW)


async def test_placeholders_are_unique_per_credential_and_totp_is_a_vault_computed_code(vault, uid):
    access = await _setup(vault, uid)
    p = await Broker(vault, user_id=uid, task_id="t1").build(access)
    assert len(p.placeholders) == len(set(p.placeholders)) == 3
    assert any(n.endswith("_2fa_code") for n in p.placeholders)
    assert not any(n.endswith("bu_2fa_code") for n in p.placeholders)      # no seed placeholder: browser-use derives nothing
    a2 = await _setup(vault, uid)
    p2 = await Broker(vault, user_id=uid, task_id="t2").build(a2)
    assert not set(p.placeholders) & set(p2.placeholders)


async def test_redact_removes_every_held_value(vault, uid):
    access = await _setup(vault, uid)
    p = await Broker(vault, user_id=uid, task_id="t1").build(access, include_cards=True)
    text = f"typed {USER} then {PW} and {SEED} card {CARD}"
    out = p.redact(text)
    for secret in (USER, PW, CARD):
        assert secret not in out
    assert SEED not in json.dumps(p.sensitive_data) and SEED not in repr(p)   # the seed never enters the agent process, so nothing to redact
    assert p.redact("harmless") == "harmless"


async def test_short_cvv_is_a_placeholder_but_not_redacted_by_value(vault, uid):
    access = await _setup(vault, uid)
    p = await Broker(vault, user_id=uid, task_id="t1").build(access, include_cards=True)
    assert any(n.endswith("card_cvv") for n in p.placeholders)
    assert p.redact(f"order 1{CVV}2") == f"order 1{CVV}2"    # would mangle ordinary text


async def test_cards_released_only_when_asked(vault, uid):
    access = await _setup(vault, uid)
    p = await Broker(vault, user_id=uid, task_id="t1").build(access)
    assert not any("card" in n for n in p.placeholders)
    assert CARD not in json.dumps(p.sensitive_data)
    p2 = await Broker(vault, user_id=uid, task_id="t1").build(access, include_cards=True)
    assert any("card_number" in n for n in p2.placeholders)


async def test_session_login_releases_no_password(vault, uid):
    access = await _setup(vault, uid)
    access.login = "session"
    p = await Broker(vault, user_id=uid, task_id="t1").build(access)
    assert p.sensitive_data == {} and p.placeholders == []


async def test_non_standard_totp_is_injected_as_a_precomputed_code_never_the_seed(vault, uid, monkeypatch):
    async def no_wait(_):
        return None
    monkeypatch.setattr("app.browser.broker.asyncio.sleep", no_wait)
    access = await _setup(vault, uid, totp_params={"digits": "8", "period": "60"})
    p = await Broker(vault, user_id=uid, task_id="t1").build(access)
    names = [n for n in p.placeholders if n.endswith("2fa_code")]
    assert len(names) == 1 and not names[0].endswith("bu_2fa_code")      # browser-use must not treat it as a seed
    values = [v for bucket in p.sensitive_data.values() for k, v in bucket.items() if k == names[0]]
    assert len(values) == 1 and values[0].isdigit() and len(values[0]) == 8 and values[0] != SEED
    assert SEED not in json.dumps(p.sensitive_data) and SEED not in repr(p)
    assert values[0] in p._values                                        # redacted by value if it ever shows up in text


async def test_one_time_value_is_a_placeholder_and_is_redacted(vault, uid):
    access = await _setup(vault, uid)
    p = await Broker(vault, user_id=uid, task_id="t1").build(access)
    p.add_one_time("swiggy.com", "otp_code", "482913")
    assert "otp_code" in p.placeholders and p.sensitive_data["https://*.swiggy.com"]["otp_code"] == "482913"
    assert "482913" not in p.redact("the code 482913 was typed") and "482913" not in repr(p)


async def test_origin_binding_is_enforced_on_every_decrypt(vault, uid):
    access = await _setup(vault, uid, site="swiggy.com")
    for c in access.credentials:           # a plan that (wrongly) points the credential at another site
        c.site = "evil.com"
    with pytest.raises(OriginDenied):
        await Broker(vault, user_id=uid, task_id="t1").build(access)


async def test_other_users_credential_is_not_found(svc, vault, uid):
    other = (await svc.tenancy.ensure_user(google_sub="g-b", email="b@x.com", name="B")).id
    access = await _setup(vault, uid)
    with pytest.raises(Exception):
        await Broker(vault, user_id=other, task_id="t1").build(access)


async def test_every_use_is_logged_and_log_holds_no_secret(vault, uid):
    access = await _setup(vault, uid)
    await Broker(vault, user_id=uid, task_id="t9").build(access)
    log = await vault.access_log(uid)
    assert len([r for r in log if r.task_id == "t9" and r.result == "allowed"]) == 2
    assert PW not in repr([r.__dict__ for r in log])


def test_the_prompt_tells_the_agent_not_to_resubmit_a_rejected_login():
    """Found live: with a wrong saved password the agent submitted the same login twice (lockout risk)."""
    p = BrokerPayload()
    p.placeholders = ["cred_ab12cd34_username", "cred_ab12cd34_password"]
    assert "request_help(kind='login')" in p.prompt_hint() and "do not submit again" in p.prompt_hint()
    assert "request_help(kind='login')" not in BrokerPayload().prompt_hint()           # no placeholders, no hint at all


async def test_phone_and_code_login_has_no_password_placeholder(vault, uid):
    """Phone + one-time-code sites (Swiggy): a login saved with only a username must work, and the model is told to
    ask for the code, not to hunt for a password."""
    cred = await vault.store(user_id=uid, kind="password", label="Swiggy", origins=["swiggy.com"],
                             secret={"username": USER, "password": ""})        # an empty password is "not given"
    access = AccessPlan(allowed_domains=["swiggy.com"], login="password",
                        credentials=[CredentialUse(credential_id=cred.id, kind="password", label="Swiggy", site="swiggy.com")])
    p = await Broker(vault, user_id=uid, task_id="t1").build(access)
    assert [n for n in p.placeholders if n.endswith("_password")] == []
    assert any(n.endswith("_username") for n in p.placeholders)
    hint = p.prompt_hint()
    assert "No password is saved" in hint and "request_help(kind='code')" in hint
    assert USER in str(p._values) and USER not in hint


async def test_username_is_still_required(vault, uid):
    with pytest.raises(VaultError):
        await vault.store(user_id=uid, kind="password", label="x", origins=["swiggy.com"], secret={"password": PW})
    with pytest.raises(VaultError):
        await vault.store(user_id=uid, kind="password", label="x", origins=["swiggy.com"], secret={"username": ""})


async def test_google_sign_in_on_another_site_releases_the_google_login_only_for_google_pages(vault, uid):
    """'Continue with Google' on canva.com: the saved google.com login is typed on Google's page, never on canva.com."""
    g = await vault.store(user_id=uid, kind="password", label="Google", origins=["google.com"],
                          secret={"username": USER, "password": PW})
    access = AccessPlan(allowed_domains=["canva.com", "google.com"], login="password", sign_in_via="google",
                        credentials=[CredentialUse(credential_id=g.id, kind="password", label="Google", site="google.com")])
    p = await Broker(vault, user_id=uid, task_id="t1").build(access)
    assert list(p.sensitive_data) == ["https://*.google.com"]               # not keyed to canva.com
    assert any(n.endswith("_password") for n in p.placeholders) and PW not in p.prompt_hint()
    # the same credential can never be released for canva.com itself
    bad = AccessPlan(allowed_domains=["canva.com"], login="password",
                     credentials=[CredentialUse(credential_id=g.id, kind="password", label="Google", site="canva.com")])
    with pytest.raises(OriginDenied):
        await Broker(vault, user_id=uid, task_id="t2").build(bad)
