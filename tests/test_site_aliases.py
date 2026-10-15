"""Gmail signs in on google.com. The saved session, the allowed domains and the health check must all use google.com."""

import pytest

from app.agent.access_planner import build_access_plan
from app.agent.schemas import Intent
from app.browser.snapshots import _cookie_site
from app.rules.engine import EffectiveRules
from app.sites.loader import canonical_site, hints_for, looks_logged_out


@pytest.mark.parametrize("name,expected", [
    ("gmail.com", "google.com"), ("GMAIL.com", "google.com"), (" gmail.com ", "google.com"), ("googlemail.com", "google.com"),
    ("google.com", "google.com"), ("swiggy.com", "swiggy.com"), ("unknown-site.io", "unknown-site.io"),
    ("gmail.com.evil.io", "gmail.com.evil.io"), ("notgmail.com", "notgmail.com"),          # lookalikes are NOT aliased
])
def test_alias_resolution(name, expected):
    assert canonical_site(name) == expected


def test_lookalike_domains_never_become_google():
    assert canonical_site("gmail.com.evil.io") == "gmail.com.evil.io"
    assert canonical_site("evilgmail.com") == "evilgmail.com"
    assert canonical_site("mail.google.com.evil.io") == "mail.google.com.evil.io"


def test_gmail_uses_the_google_hints_and_has_a_real_signed_out_check():
    h = hints_for("gmail.com")
    assert h.site == "google.com"
    assert h.auth_check_url == "https://myaccount.google.com/" and h.human_step == "google"
    assert looks_logged_out("https://accounts.google.com/v3/signin/identifier", h)    # what signed-out looks like
    assert not looks_logged_out("https://myaccount.google.com/", h)


def test_google_login_cookies_belong_to_google_dot_com_not_gmail_dot_com():
    """The bug: the snapshot filtered for gmail.com and a real sign-in produced zero matching cookies."""
    cookies = [{"domain": ".google.com", "name": "SID"}, {"domain": "mail.google.com", "name": "__Host-GMAIL_SCH_GML"},
               {"domain": "accounts.google.com", "name": "LSID"}]
    assert {_cookie_site(c) for c in cookies} == {"google.com"}
    assert canonical_site("gmail.com") in {_cookie_site(c) for c in cookies}


def rules():
    return EffectiveRules(org_id="o", user_id="u", site=None, version="v1",
                          policies={"require_approval": ["send_message"], "blocked_actions": [], "allow_card_use": True,
                                    "allow_stored_passwords": True})


def test_the_access_plan_for_gmail_allows_google_and_asks_to_connect_google():
    intent = Intent(goal="send a mail", sites=[canonical_site("gmail.com")], needs_login=True, actions=["send_message"])
    plan = build_access_plan(intent, [], rules(), session_sites=set())
    assert plan.allowed_domains == ["google.com"] and plan.connect_site == "google.com" and plan.login == "need_credentials"


def test_a_saved_google_session_is_recognised_for_a_gmail_request():
    intent = Intent(goal="read my mail", sites=[canonical_site("gmail.com")], needs_login=True)
    plan = build_access_plan(intent, [], rules(), session_sites={"google.com"})
    assert plan.login == "session" and plan.connect_site is None


# ── measured against a real expired Google session: these are the pages Google sends a signed-out browser to ──

@pytest.mark.parametrize("url", [
    "https://www.google.com/account/about/",                                           # myaccount.google.com signed out
    "https://accounts.google.com/v3/signin/accountchooser?continue=https://drive.google.com/",   # drive.google.com signed out
    "https://accounts.google.com/v3/signin/rejected?continue=https://drive.google.com/",
    "https://accounts.google.com/ServiceLogin",
])
def test_every_signed_out_google_page_is_recognised(url):
    assert looks_logged_out(url, hints_for("gmail.com"))


@pytest.mark.parametrize("url", ["https://myaccount.google.com/", "https://myaccount.google.com/?hl=en", "https://mail.google.com/mail/u/0/"])
def test_signed_in_google_pages_are_not_mistaken_for_signed_out(url):
    assert not looks_logged_out(url, hints_for("gmail.com"))


def test_google_requires_the_auth_check_to_end_on_myaccount():
    assert hints_for("gmail.com").expect_url == "myaccount.google.com"
    assert hints_for("swiggy.com").expect_url is None                    # other sites keep the generic rule
