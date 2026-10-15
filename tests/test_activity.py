"""The plain-language activity feed: readable sentences, no secrets, hides noise, newest first."""

from datetime import datetime, timedelta
from types import SimpleNamespace as NS

from app.vault import activity

T0 = datetime(2026, 10, 7, 12, 0, 0)
KEY = "sk-or-v1-abcdef0123456789abcdef0123456789"
CREDS = {"k1": NS(id="k1", kind="api_key", label="OpenRouter key"), "p1": NS(id="p1", kind="password", label="Swiggy login")}


def row(i, action, result="allowed", cred="k1", origin="https://openrouter.ai", reason=None, actor="agent:executor"):
    return NS(ts=T0 + timedelta(seconds=i), action=action, result=result, credential_id=cred, origin=origin, reason=reason, actor=actor)


def texts(rows):
    return [d["text"] for d in activity.build(rows, CREDS)]


def test_a_request_reads_as_a_sentence_with_the_outcome():
    out = texts([row(1, "request", "ok", reason="POST /api/v1/videos -> 200")])
    assert out == ["Used “OpenRouter key” on openrouter.ai: POST /api/v1/videos (it worked)"]


def test_known_failures_are_explained_not_shown_as_codes():
    assert "out of credits" in texts([row(1, "request", "error", reason="POST /x -> 402")])[0]
    assert "rejected the key" in texts([row(1, "request", "error", reason="GET /x -> 401")])[0]
    assert "couldn't reach" in texts([row(1, "request", "error", reason="POST /x -> no response")])[0]
    assert "error (500)" in texts([row(1, "request", "error", reason="GET /x -> 500")])[0]


def test_the_decrypt_row_of_an_api_key_is_hidden_but_a_refusal_is_not():
    assert texts([row(1, "inject")]) == []
    out = texts([row(1, "inject", "denied", reason="origin not allowed", origin="https://evil.com")])
    assert out == ["Blocked from using “OpenRouter key” on evil.com (origin not allowed)"]


def test_a_password_use_is_shown():
    assert texts([row(1, "inject", cred="p1", origin="https://swiggy.com")]) == ["Used “Swiggy login” on swiggy.com"]


def test_create_revoke_and_totp_say_who_did_it():
    out = texts([row(1, "create", actor="user"), row(2, "totp", cred="p1"), row(3, "revoke", actor="user")])
    assert out == ["You removed “OpenRouter key”", "Made a 2FA code from “Swiggy login”", "You saved “OpenRouter key”"]


def test_newest_first_and_limited():
    rows = [row(i, "create", actor="user") for i in range(80)]
    out = activity.build(rows, CREDS, limit=5)
    assert len(out) == 5 and out[0]["ts"] > out[-1]["ts"]


def test_a_revoked_or_unknown_credential_still_gets_a_safe_name():
    assert texts([row(1, "revoke", actor="user", cred="gone")]) == ["You removed a saved item"]


def test_failures_are_flagged_not_ok():
    d = activity.build([row(1, "request", "error", reason="POST /x -> 402")], CREDS)[0]
    assert d["ok"] is False and d["credential_id"] == "k1"


def test_no_secret_shaped_text_can_come_out():
    d = activity.build([row(1, "request", "ok", reason="GET /ok -> 200")], CREDS)
    assert KEY not in repr(d)
