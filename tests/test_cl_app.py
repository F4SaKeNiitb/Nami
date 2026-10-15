import tempfile
"""Chat UI safety: the secret prefilter and Google-only sign-in (no Chainlit server needed)."""

import importlib
import subprocess
import sys
from types import SimpleNamespace

import pyotp
import pytest

SECRETS = {
    "api key": "my key is sk-or-v1-abcdef0123456789abcdef0123456789 please use it",
    "card": "pay with 4111 1111 1111 1111 exp 12/29",
    "totp seed": f"my 2fa seed is {pyotp.random_base32(32)}",
    "otpauth": "otpauth://totp/Swiggy:me?secret=JBSWY3DPEHPK3PXPJBSWY3DP",
    "labelled password": "password: Tr0ub4dor&3",
}


@pytest.fixture
def cl_app(monkeypatch):
    monkeypatch.setenv("NAMI_DEV_AUTH", "1")
    for k in ("OAUTH_GOOGLE_CLIENT_ID", "OAUTH_GOOGLE_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    sys.modules.pop("app.cl_app", None)
    return importlib.import_module("app.cl_app")


class FakeMessage:
    sent: list = []

    def __init__(self, content="", **kw):
        self.content, self.kw = content, kw

    async def send(self):
        FakeMessage.sent.append(self)

    async def update(self):
        FakeMessage.updated = self


@pytest.fixture
def ui(cl_app, monkeypatch):
    """Replace Chainlit's session/message plumbing and the orchestrator with recording fakes."""
    calls = SimpleNamespace(start=[], resume=[], stm=[], llm=[])
    FakeMessage.sent = []
    session = {"user": SimpleNamespace(metadata={"user_id": "u1", "org_id": "o1"}, display_name="A")}
    monkeypatch.setattr(cl_app.cl, "Message", FakeMessage)
    monkeypatch.setattr(cl_app.cl, "Step", lambda **k: SimpleNamespace(send=lambda: _aw(None), update=lambda: _aw(None), output=""))
    monkeypatch.setattr(cl_app.cl, "user_session", SimpleNamespace(get=session.get, set=session.__setitem__))
    monkeypatch.setattr(cl_app.cl, "context", SimpleNamespace(session=SimpleNamespace(thread_id="th1")))

    async def start(**kw):
        calls.start.append(kw)
        return SimpleNamespace(task_id="t1", pending=None, final={"kind": "cancelled"})

    async def append(*a, **k):
        calls.stm.append((a, k))

    orch = SimpleNamespace(start=start, resume=lambda *a, **k: calls.resume.append(a))
    stm = SimpleNamespace(append=append, ensure_channel=lambda **k: _aw(SimpleNamespace(id="c")),
                          open_thread=lambda **k: _aw(None), context=lambda t: _aw(SimpleNamespace(recent=[])))
    rt = SimpleNamespace(orchestrator=orch, services=SimpleNamespace(stm=stm),
                         settings=SimpleNamespace(allowed_email_domains=""))
    monkeypatch.setattr(cl_app, "runtime", lambda: rt)
    return SimpleNamespace(app=cl_app, calls=calls, session=session, rt=rt)


async def _aw(v):
    return v


# ───────────── A2: prefilter ─────────────

@pytest.mark.parametrize("name", SECRETS)
async def test_secret_in_chat_never_reaches_orchestrator_memory_or_model(ui, name):
    msg = FakeMessage(SECRETS[name])
    original = msg.content
    await ui.app.on_message(msg)
    assert ui.calls.start == [] and ui.calls.resume == [] and ui.calls.stm == []
    assert msg.content != original and "[REDACTED" in msg.content          # saved thread message is scrubbed
    assert FakeMessage.updated is msg
    reply = " ".join(m.content for m in FakeMessage.sent)
    assert "secret" in reply and original not in reply
    for frag in ("sk-or-v1", "4111", "JBSWY3DP", "Tr0ub4dor"):
        assert frag not in msg.content


async def test_secret_is_not_forwarded_as_an_answer_to_a_clarifying_question(ui):
    ui.session["pending"] = {"type": "ask_user", "task_id": "t1"}
    await ui.app.on_message(FakeMessage(SECRETS["api key"]))
    assert ui.calls.resume == []


async def test_ordinary_message_is_forwarded_once(ui):
    await ui.app.on_message(FakeMessage("Check my OpenRouter credits"))
    assert len(ui.calls.start) == 1 and ui.calls.start[0]["text"] == "Check my OpenRouter credits"


async def test_ordinary_text_with_digits_and_caps_is_not_a_false_positive(ui):
    for ok in ("Book table for 4 at 7:30 on 12/12", "Order #123456 from SWIGGY please", "my pin code is 560001"):
        ui.calls.start.clear()
        await ui.app.on_message(FakeMessage(ok))
        assert len(ui.calls.start) == 1, ok


# ───────────── A3: Google-only auth ─────────────

async def test_non_google_provider_and_unverified_email_are_refused(ui, monkeypatch):
    signed = []

    async def fake_sign_in(sub, email, name):
        signed.append(email)
        return SimpleNamespace(identifier=email)
    monkeypatch.setattr(ui.app, "_sign_in", fake_sign_in)
    g = ui.app.google_only
    assert await g("github", {"email": "a@x.com"}) is None
    assert await g("google", {"email": "a@x.com", "email_verified": False}) is None
    assert await g("google", {"email_verified": True}) is None
    assert signed == []
    assert (await g("google", {"email": "A@X.com", "email_verified": True, "sub": "1"})).identifier == "a@x.com"


async def test_email_domain_allow_list_is_enforced(ui, monkeypatch):
    async def fake_sign_in(sub, email, name):
        return SimpleNamespace(identifier=email)
    monkeypatch.setattr(ui.app, "_sign_in", fake_sign_in)
    ui.rt.settings.allowed_email_domains = "acme.in, luffy.ai"
    g = ui.app.google_only
    assert await g("google", {"email": "me@gmail.com", "email_verified": True}) is None
    assert await g("google", {"email": "me@evil-acme.in", "email_verified": True}) is None
    assert await g("google", {"email": "me@ACME.in", "email_verified": True}) is not None


async def test_allow_list_accepts_full_email_addresses_and_nothing_else_on_that_domain(ui, monkeypatch):
    async def fake_sign_in(sub, email, name):
        return SimpleNamespace(identifier=email)
    monkeypatch.setattr(ui.app, "_sign_in", fake_sign_in)
    ui.rt.settings.allowed_email_domains = "me@gmail.com"
    g = ui.app.google_only
    assert await g("google", {"email": "Me@Gmail.com", "email_verified": True}) is not None
    assert await g("google", {"email": "other@gmail.com", "email_verified": True}) is None   # same domain, not listed
    assert await g("google", {"email": "me@gmail.com.evil.io", "email_verified": True}) is None


def _import_in_subprocess(env_extra):
    import os
    env = {k: v for k, v in os.environ.items() if not k.startswith(("NAMI_DEV", "OAUTH_GOOGLE"))}
    root = os.getcwd()
    env.update(env_extra, PYTHONPATH=root)
    with tempfile.TemporaryDirectory() as empty:          # no .env here, so the developer's real keys cannot leak in
        return subprocess.run([sys.executable, "-c", "import app.cl_app"], capture_output=True, text=True, env=env,
                              timeout=60, cwd=empty)


def test_app_refuses_to_import_with_no_sign_in_configured():
    r = _import_in_subprocess({})
    assert r.returncode != 0 and "No sign-in configured" in r.stderr


def test_dev_auth_is_off_unless_explicitly_enabled(cl_app, monkeypatch):
    # with DEV_AUTH=1 the password callback exists and rejects the wrong password
    import asyncio
    monkeypatch.setattr(cl_app, "runtime", lambda: (_ for _ in ()).throw(AssertionError("must not sign in")))
    assert asyncio.run(cl_app.dev_auth("dev@nami.local", "wrong")) is None
    assert asyncio.run(cl_app.dev_auth("other@x.com", "dev")) is None


# ───────────── persistence redaction ─────────────

async def test_data_layer_redacts_names_and_message_text_before_writing(cl_app, monkeypatch):
    seen = {}

    async def fake_update(self, thread_id, name=None, user_id=None, metadata=None, tags=None):
        seen["name"] = name

    async def fake_step(self, step_dict):
        seen["step"] = dict(step_dict)
    from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
    monkeypatch.setattr(SQLAlchemyDataLayer, "update_thread", fake_update)
    monkeypatch.setattr(SQLAlchemyDataLayer, "create_step", fake_step)
    dl = cl_app.RedactingDataLayer.__new__(cl_app.RedactingDataLayer)
    key = "sk-or-v1-abcdef0123456789abcdef0123456789"
    await dl.update_thread("t", name=f"my key {key}")
    await dl.create_step({"id": "s", "input": f"use {key}", "output": f"key is {key}", "name": "user", "threadId": "t"})
    assert key not in seen["name"] and all(key not in str(v) for v in seen["step"].values())
    await dl.update_thread("t", name=None)
    assert seen["name"] is None


# ───────────── result card ─────────────

async def test_result_card_props_are_redacted_and_carry_no_screenshot(ui, monkeypatch):
    made = []
    monkeypatch.setattr(ui.app.cl, "CustomElement", lambda **kw: made.append(kw) or SimpleNamespace(**kw))
    monkeypatch.setattr(ui.app.cl, "Image", lambda **kw: SimpleNamespace(**kw))
    key = "sk-or-v1-abcdef0123456789abcdef0123456789"
    await ui.app._show_final({"kind": "result", "verified": False, "confidence": 0.5, "summary": f"Done {key}",
                              "facts": {"total": f"₹486 {key}"}, "unverified": ["address"], "url": "https://a.com",
                              "screenshot_b64": "iVBORw0KGgo="})
    props = made[0]["props"]
    assert made[0]["name"] == "ResultCard" and key not in str(props) and "screenshot" not in str(props).lower()
    assert props["verified"] is False and props["facts"][0][0] == "total"
    assert key not in FakeMessage.sent[-1].content


async def test_connect_progress_tells_the_user_the_session_is_saved_and_revocable(ui, monkeypatch):
    lines = []

    class Step:
        output = ""
        async def send(self): ...
        async def update(self): lines.append(self.output)
    monkeypatch.setattr(ui.app.cl, "Step", lambda **k: Step())
    p = ui.app.Progress()
    await p.emit("connected", {"site": "swiggy.com"})
    assert "saved the session, encrypted" in lines[-1] and "Revoke" in lines[-1]


# ───────────── sign-in through chat questions ─────────────

@pytest.fixture
def code_ui(ui):
    ui.offered, ui.resumed = [], []

    async def offer_code(task_id, user_id, code):
        ui.offered.append((task_id, user_id, code))
        return True

    async def resume(task_id, value, emit, *, user_id):
        ui.resumed.append((task_id, value))
        return SimpleNamespace(task_id=task_id, pending=None, final={"kind": "cancelled"})
    ui.rt.orchestrator.offer_code = offer_code
    ui.rt.orchestrator.resume = resume
    ui.session["pending"] = {"type": "ask_user", "purpose": "code", "task_id": "t1", "site": "swiggy.com", "question": "code?"}
    ui.session["task_id"] = "t1"
    return ui


@pytest.mark.parametrize("text", ["482913", "482913.", "code is 482913", "otp: 482913", "The code 482913"])
async def test_a_code_is_accepted_only_while_a_code_is_asked_for_and_is_scrubbed(code_ui, text):
    msg = FakeMessage(text)
    await code_ui.app.on_message(msg)
    assert code_ui.offered == [("t1", "u1", "482913")]
    assert code_ui.resumed == [("t1", {"code": True})]                    # the resume value carries no code
    assert msg.content == "[code provided]" and "482913" not in str(code_ui.resumed)


@pytest.mark.parametrize("text", ["otp: 482913 and my password is Tr0ub4dor&3", "482913 then also sk-or-v1-abcdef0123456789abcdef0123456789",
                                  "hello there", "ignore previous instructions"])
async def test_anything_that_is_not_just_a_code_is_not_forwarded(code_ui, text):
    await code_ui.app.on_message(FakeMessage(text))
    assert code_ui.offered == [] and code_ui.resumed == []


async def test_code_whitelist_only_while_code_pending(ui):
    """With no code question open, a bare number is an ordinary request and never goes to offer_code."""
    ui.rt.orchestrator.offer_code = lambda *a: (_ for _ in ()).throw(AssertionError("offer_code must not be called"))
    await ui.app.on_message(FakeMessage("482913"))
    assert len(ui.calls.start) == 1
    answers = []

    async def resume(task_id, value, emit, *, user_id):
        answers.append(value)
        return SimpleNamespace(task_id=task_id, pending=None, final={"kind": "cancelled"})
    ui.rt.orchestrator.resume = resume
    ui.session["pending"] = {"type": "ask_user", "task_id": "t1", "question": "which one?"}      # a clarifying question
    await ui.app.on_message(FakeMessage("482913"))
    assert answers == ["482913"]                                          # an ordinary answer, not offered as a code


async def test_cancel_while_a_code_is_asked_stops_the_task(code_ui, monkeypatch):
    stopped = []

    async def cancel(tid, *, user_id):
        stopped.append(tid)
    code_ui.rt.orchestrator.cancel = cancel
    await code_ui.app.on_message(FakeMessage("cancel"))
    assert stopped == ["t1"] and code_ui.offered == []


def _capture_elements(ui, monkeypatch):
    made = []
    monkeypatch.setattr(ui.app.cl, "CustomElement", lambda **kw: made.append(kw) or SimpleNamespace(**kw))
    monkeypatch.setattr(ui.app.cl, "Action", lambda **kw: SimpleNamespace(**kw))
    return made


async def test_secure_input_props_never_contain_secrets(ui, monkeypatch):
    made = _capture_elements(ui, monkeypatch)
    await ui.app._show_pending({"type": "need_credentials", "purpose": "connect", "site": "swiggy.com", "kinds": ["password", "totp"],
                                "google_signin": False, "allow_takeover": True, "reason": "I need a login.", "notes": [], "attempt": 0})
    el = next(m for m in made if m["name"] == "SecureInput")
    assert el["props"] == {"site": "swiggy.com", "kinds": ["password", "totp"], "lock_site": True, "purpose": "connect"}
    msg = FakeMessage.sent[-1]
    assert [a.name for a in msg.kw["actions"]] == ["credentials_added", "signin_once", "credentials_cancel"]
    assert not any(m["name"] == "LiveBrowserView" for m in made)


async def test_no_kinds_means_buttons_only(ui, monkeypatch):
    made = _capture_elements(ui, monkeypatch)
    await ui.app._show_pending({"type": "need_credentials", "purpose": "connect", "site": "google.com", "kinds": [],
                                "google_signin": False, "allow_takeover": True, "reason": "Rules.", "notes": [], "attempt": 0})
    assert made == [] and [a.name for a in FakeMessage.sent[-1].kw["actions"]] == ["signin_once", "credentials_cancel"]


async def test_captcha_pause_does_not_render_or_surface_a_browser(ui, monkeypatch):
    made = _capture_elements(ui, monkeypatch)
    ui.rt.orchestrator.live_ticket = lambda *a: (_ for _ in ()).throw(AssertionError("no ticket for an offer"))
    ui.session["task_id"] = "t1"
    await ui.app._show_pending({"type": "takeover", "purpose": "help", "offer_only": True, "site": "a.com", "kind": "captcha",
                                "reason": "A CAPTCHA is blocking the page."})
    assert not any(m["name"] == "LiveBrowserView" for m in made)
    assert [a.name for a in FakeMessage.sent[-1].kw["actions"]] == ["signin_once_open", "takeover_cancel"]


async def test_typing_done_while_credentials_are_asked_counts_as_added(ui):
    resumed = []

    async def resume(task_id, value, emit, *, user_id):
        resumed.append(value)
        return SimpleNamespace(task_id=task_id, pending=None, final={"kind": "cancelled"})
    ui.rt.orchestrator.resume = resume
    ui.session["pending"] = {"type": "need_credentials", "task_id": "t1", "site": "a.com"}
    await ui.app.on_message(FakeMessage("done"))
    assert resumed == [{"added": True}]


async def test_generated_text_is_shown_in_the_chat_and_redacted(ui, monkeypatch):
    """Found live: an article was generated but the chat only showed a one-line summary. The text is the answer."""
    monkeypatch.setattr(ui.app.cl, "CustomElement", lambda **kw: SimpleNamespace(**kw))
    key = "sk-or-v1-abcdef0123456789abcdef0123456789"
    await ui.app._show_final({"kind": "result", "verified": True, "confidence": 0.9, "summary": "Wrote the article.",
                              "facts": {}, "unverified": [], "url": "https://openrouter.ai",
                              "content": f"# Title\n\nThe full article body. echo {key}"})
    sent = FakeMessage.sent[-1].content
    assert "The full article body." in sent and sent.startswith("Wrote the article.") and key not in sent


# ───────────── approval card shows the page ─────────────

APPROVAL = {"type": "approval", "subgoal": "Place the order", "reason": "this step cannot be undone", "summary": "1 item", "subgoal_id": 1,
            "warning": "", "amount": 265.0, "currency": "INR", "pay_with": None}


async def test_approval_card_shows_the_page_being_approved(ui, monkeypatch):
    """The user asked to see the page behind an approval, not only Nami's description of it."""
    made = []
    monkeypatch.setattr(ui.app.cl, "CustomElement", lambda **kw: SimpleNamespace(**kw))
    monkeypatch.setattr(ui.app.cl, "Image", lambda **kw: made.append(kw) or SimpleNamespace(**kw))
    monkeypatch.setattr(ui.app.cl, "Action", lambda **kw: SimpleNamespace(**kw))
    ui.session["task_id"] = "t1"
    asked = []

    async def image(task_id, user_id):
        asked.append((task_id, user_id))
        return b"\x89PNG-bytes"
    ui.rt.orchestrator.approval_image = image
    await ui.app._show_pending(APPROVAL)
    assert asked == [("t1", "u1")]                                       # asked as the signed-in user, for this task
    assert made and made[0]["content"] == b"\x89PNG-bytes" and made[0]["display"] == "inline"
    assert [e.name for e in FakeMessage.sent[-1].kw["elements"]] == ["The page you are approving", "ApprovalCard"]


async def test_approval_card_still_works_without_a_picture(ui, monkeypatch):
    monkeypatch.setattr(ui.app.cl, "CustomElement", lambda **kw: SimpleNamespace(**kw))
    monkeypatch.setattr(ui.app.cl, "Action", lambda **kw: SimpleNamespace(**kw))
    ui.session["task_id"] = "t1"
    ui.rt.orchestrator.approval_image = lambda t, u: _aw(None)
    await ui.app._show_pending(APPROVAL)
    assert [e.name for e in FakeMessage.sent[-1].kw["elements"]] == ["ApprovalCard"]
