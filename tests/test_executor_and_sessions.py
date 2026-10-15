"""Executor boundaries (with a fake browser-use Agent) and the session manager (encrypted snapshots, health check)."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.agent.guard import Guard
from app.agent.schemas import AccessPlan, Subgoal
from app.browser import executor as ex
from app.browser import session_manager as sm
from app.browser.broker import BrokerPayload
from app.browser.provider import BrowserSession
from app.browser.snapshots import SiteSnapshot
from app.sites.loader import SiteHints
from app.vault.kms import LocalKms
from app.vault.service import VaultService

PW = "Tr0ub4dor&3-unique-marker"


# ───────────── A8: executor ─────────────

class FakeHistory:
    def __init__(self, ok=True, result="done it", errors=()):
        self.ok, self.result, self._errors = ok, result, list(errors)

    def urls(self): return ["https://a.com/x"]
    def final_result(self): return self.result
    def number_of_steps(self): return 2
    def is_done(self): return self.ok
    def is_successful(self): return self.ok
    def errors(self): return self._errors
    def model_thoughts(self): return [SimpleNamespace(next_goal=f"typing {PW}")]


class FakeAgent:
    """Stands in for browser_use.Agent: records its constructor args and runs a scripted behaviour."""
    last = None
    behaviour = staticmethod(lambda agent, hooks: FakeHistory())

    def __init__(self, **kw):
        self.kw, self.stopped = kw, False
        self.browser_session = SimpleNamespace(get_current_page_url=self._url)
        self.url = "https://a.com/x"
        self.history = FakeHistory()
        FakeAgent.last = self

    async def _url(self):
        return self.url

    def stop(self):
        self.stopped = True

    async def run(self, max_steps, on_step_start, on_step_end):
        return await FakeAgent.behaviour(self, (on_step_start, on_step_end))


class FakeBrowser:
    last = None

    def __init__(self, **kw):
        self.kw = kw
        FakeBrowser.last = self

    async def stop(self): ...


@pytest.fixture
def run(monkeypatch):
    monkeypatch.setattr(ex, "Agent", FakeAgent)
    monkeypatch.setattr(ex, "Browser", FakeBrowser)
    monkeypatch.setattr(ex, "AutoToolChoiceAnthropic", lambda **kw: object())
    _page(monkeypatch, "https://a.com/x")                 # the real observe() would try to attach to the fake CDP address
    FakeAgent.behaviour = staticmethod(lambda agent, hooks: _done())

    async def go(subgoal=None, access=None, payload=None, approval_actions=(), one_time_note=""):
        sg = subgoal or Subgoal(id=1, goal="read balance", success_criterion="balance shown")
        acc = access or AccessPlan(allowed_domains=["a.com"], approval_actions=list(approval_actions))
        events = []

        async def emit(t, p):
            events.append((t, p))
        res = await ex.Executor(anthropic_api_key="k", model="m").run(
            session=BrowserSession(cdp_url="ws://x", session_id="s", user_id="u"), subgoal=sg, access=acc, guard=Guard(acc),
            payload=payload or BrokerPayload(), rules_block="", emit=emit, one_time_note=one_time_note)
        return res, events
    return go


async def _done():
    return FakeHistory()


async def test_navigation_is_limited_to_plan_domains(run):
    await run(access=AccessPlan(allowed_domains=["a.com", "b.org"]))
    assert FakeBrowser.last.kw["allowed_domains"] == ["a.com", "*.a.com", "b.org", "*.b.org"]
    assert FakeBrowser.last.kw["cdp_url"] == "ws://x"


@pytest.mark.parametrize("is_auth,vision", [(True, False), (False, "auto")])
async def test_vision_is_off_on_sign_in_steps(run, is_auth, vision):
    await run(subgoal=Subgoal(id=1, goal="sign in", success_criterion="c", is_auth=is_auth))
    assert FakeAgent.last.kw["use_vision"] == vision


async def test_secret_values_are_never_in_the_task_or_system_message(run):
    payload = BrokerPayload(sensitive_data={"https://*.a.com": {"cred_x_password": PW}}, placeholders=["cred_x_password"], _values=[PW])
    await run(payload=payload)
    kw = FakeAgent.last.kw
    assert PW not in kw["task"] and PW not in kw["extend_system_message"]
    assert "<secret>cred_x_password</secret>" in kw["extend_system_message"]
    assert kw["sensitive_data"] == {"https://*.a.com": {"cred_x_password": PW}}
    assert "untrusted" in kw["extend_system_message"].lower()


async def test_progress_events_are_redacted(run):
    async def behaviour(agent, hooks):
        await hooks[1](agent)
        return FakeHistory()
    FakeAgent.behaviour = staticmethod(behaviour)
    _, events = await run(payload=BrokerPayload(_values=[PW]))
    steps = [p["text"] for t, p in events if t == "step"]
    assert steps and PW not in steps[0] and "[redacted]" in steps[0]


async def test_backstop_stops_the_run_at_a_payment_page(run):
    async def behaviour(agent, hooks):
        agent.url = "https://a.com/checkout/pay"
        await hooks[0](agent)
        return FakeHistory(ok=False)
    FakeAgent.behaviour = staticmethod(behaviour)
    res, _ = await run(approval_actions=["purchase"])
    assert FakeAgent.last.stopped and res.status == "needs_approval"


async def test_backstop_is_quiet_for_read_only_tasks(run):
    async def behaviour(agent, hooks):
        agent.url = "https://a.com/checkout/pay"
        await hooks[0](agent)
        return FakeHistory()
    FakeAgent.behaviour = staticmethod(behaviour)
    res, _ = await run()
    assert not FakeAgent.last.stopped and res.status == "done"


async def test_request_help_pauses_and_redacts_the_reason(run):
    async def behaviour(agent, hooks):
        fn = agent.kw["tools"].registry.registry.actions["request_help"].function
        await fn(kind="login", reason=f"login needs password {PW}")
        return FakeHistory(ok=False)
    FakeAgent.behaviour = staticmethod(behaviour)
    res, _ = await run(payload=BrokerPayload(_values=[PW]))
    assert res.status == "needs_user" and res.help_kind == "login" and PW not in res.reason


@pytest.mark.parametrize("kind,expected", [("code", "code"), ("CAPTCHA", "captcha"), (" passkey ", "passkey"), ("login", "login"),
                                           ("bank-transfer", "other"), ("", "other")])
async def test_request_help_kinds_are_normalised(run, kind, expected):
    async def behaviour(agent, hooks):
        fn = agent.kw["tools"].registry.registry.actions["request_help"].function
        await fn(kind=kind, reason="blocked")
        return FakeHistory(ok=False)
    FakeAgent.behaviour = staticmethod(behaviour)
    res, _ = await run()
    assert res.status == "needs_user" and res.help_kind == expected


async def test_one_time_note_reaches_the_prompt_but_the_code_does_not(run):
    code = "482913"
    payload = BrokerPayload()
    payload.add_one_time("a.com", "otp_code", code)
    await run(payload=payload, one_time_note=ex.ONE_TIME_NOTE)
    kw = FakeAgent.last.kw
    assert "<secret>otp_code</secret>" in kw["task"] and "<secret>otp_code</secret>" in kw["extend_system_message"]
    assert code not in kw["task"] and code not in kw["extend_system_message"]
    assert kw["sensitive_data"] == {"https://*.a.com": {"otp_code": code}}


async def test_no_note_means_no_code_instruction(run):
    await run()
    assert "verification code" not in FakeAgent.last.kw["task"]


class _FinishedButNotSuccessful(FakeHistory):
    def is_done(self): return True
    def is_successful(self): return False


@pytest.mark.parametrize("report,expected", [("There are no free video models: all 29 listed models are paid, from $0.02 per second up to $0.40 per second.", "done"),
                                             ("could not find it", "failed")])
async def test_a_finished_report_goes_to_the_validator_even_if_flagged_unsuccessful(run, report, expected):
    """A correct 'there are none' used to be filed as a failure and retried until the budget ran out."""
    FakeAgent.behaviour = staticmethod(lambda agent, hooks: _aw_value(_FinishedButNotSuccessful(result=report)))
    res, _ = await run()
    assert res.status == expected and (expected != "done" or res.claim == report)


async def _aw_value(v):
    return v


async def test_failure_reasons_and_exceptions_are_redacted(run):
    FakeAgent.behaviour = staticmethod(lambda agent, hooks: _done_failed())
    res, _ = await run(payload=BrokerPayload(_values=[PW]))
    assert res.status == "failed" and PW not in res.reason

    async def boom(agent, hooks):
        raise RuntimeError(f"timeout while typing {PW}")
    FakeAgent.behaviour = staticmethod(boom)
    res, _ = await run(payload=BrokerPayload(_values=[PW]))
    assert res.status == "failed" and PW not in res.reason


async def _done_failed():
    return FakeHistory(ok=False, errors=[f"element rejected {PW}"])


# ───────────── A9: session manager ─────────────

@pytest.fixture
async def mgr(svc, monkeypatch):
    vault = VaultService(svc.db, LocalKms())
    store = {}
    snap = SiteSnapshot(site="acme.com", cookies=[{"name": "sid", "value": "COOKIE-SECRET", "domain": "acme.com"}], local_storage={})

    async def snapshot_site(url, site): return snap
    async def restore_site(url, s): store["restored"] = s
    async def clear_site(url, site): store["cleared"] = site
    monkeypatch.setattr(sm, "snapshot_site", snapshot_site)
    monkeypatch.setattr(sm, "restore_site", restore_site)
    monkeypatch.setattr(sm, "clear_site", clear_site)
    users = [(await svc.tenancy.ensure_user(google_sub=f"g{i}", email=f"{i}@x.com", name="n")).id for i in range(2)]
    return SimpleNamespace(m=sm.SessionManager(svc.db, vault), store=store, a=users[0], b=users[1], db=svc.db)


async def test_snapshot_is_encrypted_at_rest(mgr):
    assert await mgr.m.capture(mgr.a, "ws://x", "acme.com")
    from sqlalchemy import select
    from app.db.models import BrowserSnapshot
    async with mgr.db.session() as s:
        row = (await s.scalars(select(BrowserSnapshot))).one()
    assert b"COOKIE-SECRET" not in row.ciphertext and b"sid" not in row.ciphertext


async def test_round_trip_restore_and_per_user_isolation(mgr):
    await mgr.m.capture(mgr.a, "ws://x", "acme.com")
    assert await mgr.m.sites(mgr.a) == {"acme.com"} and await mgr.m.sites(mgr.b) == set()
    assert await mgr.m.restore(mgr.a, "ws://x", "acme.com")
    assert mgr.store["restored"].cookies[0]["value"] == "COOKIE-SECRET"
    mgr.store.clear()
    assert not await mgr.m.restore(mgr.b, "ws://x", "acme.com") and "restored" not in mgr.store


async def test_tampered_ciphertext_is_not_restored(mgr):
    await mgr.m.capture(mgr.a, "ws://x", "acme.com")
    from sqlalchemy import select
    from app.db.models import BrowserSnapshot
    async with mgr.db.session() as s:
        row = (await s.scalars(select(BrowserSnapshot))).one()
        row.ciphertext = row.ciphertext[:-1] + bytes([row.ciphertext[-1] ^ 1])
    assert not await mgr.m.restore(mgr.a, "ws://x", "acme.com") and "restored" not in mgr.store


async def test_snapshot_cannot_be_replayed_under_another_site_name(mgr):
    await mgr.m.capture(mgr.a, "ws://x", "acme.com")
    from sqlalchemy import select
    from app.db.models import BrowserSnapshot
    async with mgr.db.session() as s:
        (await s.scalars(select(BrowserSnapshot))).one().site = "evil.com"      # AAD includes the site
    assert not await mgr.m.restore(mgr.a, "ws://x", "evil.com")


async def test_revoke_removes_snapshot_and_clears_live_cookies(mgr):
    await mgr.m.capture(mgr.a, "ws://x", "acme.com")
    await mgr.m.revoke(mgr.a, "acme.com", cdp_url="ws://x")
    assert await mgr.m.sites(mgr.a) == set() and mgr.store["cleared"] == "acme.com"
    assert not await mgr.m.restore(mgr.a, "ws://x", "acme.com")


async def test_nothing_to_capture_is_a_noop(mgr, monkeypatch):
    async def empty(url, site): return SiteSnapshot(site=site, cookies=[], local_storage={})
    monkeypatch.setattr(sm, "snapshot_site", empty)
    assert not await mgr.m.capture(mgr.a, "ws://x", "acme.com") and await mgr.m.sites(mgr.a) == set()


class FakePage:
    def __init__(self, final_url, has_password):
        self.url, self.pw = final_url, has_password

    async def goto(self, url, **kw): ...
    async def close(self): ...
    def locator(self, sel):
        return SimpleNamespace(count=lambda: _n(1 if self.pw else 0))


async def _n(v): return v


def fake_connect(page):
    @asynccontextmanager
    async def connect(url):
        yield SimpleNamespace(contexts=[SimpleNamespace(new_page=lambda: _n(page))])
    return connect


@pytest.mark.parametrize("final_url,pw,logged_in", [("https://acme.com/home", False, True), ("https://acme.com/login", False, False),
                                                    ("https://acme.com/home", True, False), ("https://accounts.google.com/x", False, False)])
async def test_health_check_detects_login_redirect_and_password_box(mgr, monkeypatch, final_url, pw, logged_in):
    from app.browser import cdp
    monkeypatch.setattr(cdp, "connect", fake_connect(FakePage(final_url, pw)))
    hints = SiteHints(site="acme.com", auth_check_url="https://acme.com/account", login_patterns=("/login", "accounts.google.com"))
    assert await mgr.m.is_logged_in("ws://x", hints) is logged_in


async def test_site_without_auth_check_url_is_assumed_fine(mgr):
    assert await mgr.m.is_logged_in("ws://x", SiteHints(site="x.com"))


# ── limits and grounding ──

async def test_a_stuck_run_is_stopped_after_the_timeout_and_reported_honestly(monkeypatch):
    import asyncio
    _page(monkeypatch, "https://a.com/x")
    monkeypatch.setattr(ex, "Agent", FakeAgent)
    monkeypatch.setattr(ex, "Browser", FakeBrowser)
    monkeypatch.setattr(ex, "AutoToolChoiceAnthropic", lambda **kw: object())

    async def hang(agent, hooks):
        await asyncio.sleep(30)
    FakeAgent.behaviour = staticmethod(hang)
    acc = AccessPlan(allowed_domains=["a.com"])
    res = await ex.Executor(anthropic_api_key="k", model="m", timeout_seconds=0.05).run(
        session=BrowserSession(cdp_url="ws://x", session_id="s", user_id="u"),
        subgoal=Subgoal(id=1, goal="g", success_criterion="c"), access=acc, guard=Guard(acc),
        payload=BrokerPayload(), rules_block="", emit=lambda *a: asyncio.sleep(0))
    assert res.status == "failed" and "longer than" in res.reason and "stopped" in res.reason


async def test_the_browser_is_disconnected_even_when_the_run_times_out(monkeypatch):
    import asyncio
    _page(monkeypatch, "https://a.com/x")
    stopped = []

    class B(FakeBrowser):
        async def stop(self):
            stopped.append(1)
    monkeypatch.setattr(ex, "Agent", FakeAgent)
    monkeypatch.setattr(ex, "Browser", B)
    monkeypatch.setattr(ex, "AutoToolChoiceAnthropic", lambda **kw: object())

    async def hang(agent, hooks):
        await asyncio.sleep(30)
    FakeAgent.behaviour = staticmethod(hang)
    acc = AccessPlan(allowed_domains=["a.com"])
    await ex.Executor(anthropic_api_key="k", model="m", timeout_seconds=0.05).run(
        session=BrowserSession(cdp_url="ws://x", session_id="s", user_id="u"),
        subgoal=Subgoal(id=1, goal="g", success_criterion="c"), access=acc, guard=Guard(acc),
        payload=BrokerPayload(), rules_block="", emit=lambda *a: asyncio.sleep(0))
    assert stopped == [1]


async def test_the_executor_is_told_to_report_only_what_is_on_the_page_now(run):
    await run()
    msg = FakeAgent.last.kw["extend_system_message"]
    assert "Report only what you can see on the current page" in msg and "never rely on an earlier extraction" in msg
    assert msg.index("untrusted") < msg.index("Report only")          # the injection rule still comes first


def test_default_limits_are_bounded():
    from app.core.config import Settings
    s = Settings()
    assert 0 < s.subgoal_timeout_seconds <= 600 and 0 < s.task_max_steps <= 100 and 0 < s.task_max_seconds <= 1800


# ── later subgoals continue from where the earlier ones left the browser ──

def _page(monkeypatch, url):
    async def fake_observe(cdp_url, prefer_url, *, with_text, with_shot):
        return url, "", None
    monkeypatch.setattr(ex, "observe", fake_observe)


async def test_browser_use_is_not_allowed_to_open_a_url_found_in_the_task_text(run):
    """It did: 'Stay on: google.com' made every subgoal start with navigate(google.com), wiping the open page."""
    await run()
    assert FakeAgent.last.kw["directly_open_url"] is False


async def test_the_agent_is_told_which_page_is_already_open(run, monkeypatch):
    _page(monkeypatch, "https://drive.google.com/file/d/abc/view")
    await run()
    task = FakeAgent.last.kw["task"]
    assert "already open on https://drive.google.com/file/d/abc/view" in task and "do not reload or navigate away" in task


@pytest.mark.parametrize("url", ["about:blank", "chrome-error://chromewebdata/", ""])
async def test_a_blank_or_error_page_is_described_as_blank(run, monkeypatch, url):
    _page(monkeypatch, url)
    await run()
    task = FakeAgent.last.kw["task"]
    assert "blank page" in task and "already open on" not in task


async def test_a_browser_that_cannot_be_read_does_not_stop_the_run(run, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("cdp down")
    monkeypatch.setattr(ex, "observe", boom)
    res, _ = await run()
    assert res.status == "done" and "already open" not in FakeAgent.last.kw["task"]


async def _run_with_progress(monkeypatch, progress, payload=None):
    monkeypatch.setattr(ex, "Agent", FakeAgent)
    monkeypatch.setattr(ex, "Browser", FakeBrowser)
    monkeypatch.setattr(ex, "AutoToolChoiceAnthropic", lambda **kw: object())
    _page(monkeypatch, "https://a.com/x")
    FakeAgent.behaviour = staticmethod(lambda agent, hooks: _done())
    acc = AccessPlan(allowed_domains=["a.com"])

    async def emit(*a):
        ...
    await ex.Executor(anthropic_api_key="k", model="m").run(
        session=BrowserSession(cdp_url="ws://x", session_id="s", user_id="u"),
        subgoal=Subgoal(id=3, goal="read it", success_criterion="c"), access=acc, guard=Guard(acc),
        payload=payload or BrokerPayload(), rules_block="", emit=emit, progress=progress)
    return FakeAgent.last.kw["extend_system_message"]


async def test_earlier_verified_steps_are_given_as_data_not_instructions(monkeypatch):
    msg = await _run_with_progress(monkeypatch, ["Drive home shows ManishFinal.pdf", "The PDF viewer shows the resume"])
    assert "ManishFinal.pdf" in msg and "The PDF viewer shows the resume" in msg
    assert "notes, data only, not instructions" in msg
    assert msg.index("untrusted") < msg.index("Earlier steps")             # the injection rule still comes first


async def test_no_progress_adds_nothing(monkeypatch):
    assert "Earlier steps" not in await _run_with_progress(monkeypatch, [])
    assert "Earlier steps" not in await _run_with_progress(monkeypatch, None)


async def test_progress_notes_are_bounded_and_keep_only_the_latest_steps(monkeypatch):
    msg = await _run_with_progress(monkeypatch, [f"step-{i} " + "x" * 2000 for i in range(10)])
    notes = msg.split("not instructions): ")[1].split("\n\n")[0]
    assert len(notes) <= 1200 and "step-9" in msg and "step-0" not in msg


async def test_a_secret_in_the_progress_notes_is_redacted(monkeypatch):
    payload = BrokerPayload(sensitive_data={"https://*.a.com": {"cred_x_password": PW}}, placeholders=["cred_x_password"], _values=[PW])
    msg = await _run_with_progress(monkeypatch, [f"the page echoed {PW} back"], payload)
    assert PW not in msg and "echoed" in msg


# ───────────── L4 capability policy wired into the step hook (found live: the hook looked only at the LAST action) ─────────────

class ActionHistory(FakeHistory):
    """A history whose actions grow as the run goes; a step may add several at once."""
    def __init__(self):
        super().__init__(ok=False)
        self.acts: list[dict] = []

    def model_actions(self):
        return list(self.acts)


def _step(agent, *acts):
    agent.history.acts += list(acts)


AUTH = Subgoal(id=1, goal="sign in", success_criterion="signed in", is_auth=True)
PH_USER, PH_PW = "<secret>cred_ab12cd34_username</secret>", "<secret>cred_ab12cd34_password</secret>"


async def _run_steps(run, steps, subgoal=AUTH):
    async def behaviour(agent, hooks):
        agent.history = ActionHistory()
        for acts in steps:
            _step(agent, *acts)
            await hooks[1](agent)
        return agent.history
    FakeAgent.behaviour = staticmethod(behaviour)
    return await run(subgoal=subgoal)


async def test_a_literal_typed_before_a_click_in_the_same_step_is_still_checked(run):
    """Two actions in one step: the old hook looked only at the last (the click) and let the typed literal through."""
    res, events = await _run_steps(run, [[{"input_text": {"index": 3, "text": "hunter2"}}, {"click_element_by_index": {"index": 4}}]])
    assert res.status == "failed" and "stopped by policy" in res.reason
    assert [e for e in events if e[0] == "capability_denied"] and FakeAgent.last.stopped


async def test_placeholders_typed_in_a_sign_in_step_are_allowed(run):
    res, events = await _run_steps(run, [[{"input_text": {"index": 3, "text": PH_USER}}, {"input_text": {"index": 4, "text": PH_PW}},
                                          {"click_element_by_index": {"index": 5}}]])
    assert not [e for e in events if e[0] == "capability_denied"] and not FakeAgent.last.stopped


async def test_pressing_enter_to_submit_a_sign_in_is_allowed(run):
    res, events = await _run_steps(run, [[{"input_text": {"index": 4, "text": PH_PW}}, {"send_keys": {"keys": "Enter"}}]])
    assert not [e for e in events if e[0] == "capability_denied"] and not FakeAgent.last.stopped


async def test_a_string_sent_as_keys_in_a_sign_in_step_is_refused(run):
    res, events = await _run_steps(run, [[{"send_keys": {"keys": "hunter2"}}]])
    assert [e for e in events if e[0] == "capability_denied"] and res.status == "failed"


async def test_a_wrong_saved_password_is_not_typed_a_third_time(run):
    """Found live: with a wrong saved password the agent submitted the same login three times (lockout risk)."""
    typed = {"input_text": {"index": 4, "text": PH_PW}}
    res, events = await _run_steps(run, [[typed], [typed], [typed]])
    denied = [e for e in events if e[0] == "capability_denied"]
    assert len(denied) == 1 and "typed more than twice" in denied[0][1]["reason"]
    assert res.status == "failed" and FakeAgent.last.stopped


async def test_typing_the_password_twice_is_still_allowed(run):
    typed = {"input_text": {"index": 4, "text": PH_PW}}
    res, events = await _run_steps(run, [[typed], [typed]])
    assert not [e for e in events if e[0] == "capability_denied"]


async def test_navigating_off_the_planned_sites_stops_the_run(run):
    res, events = await _run_steps(run, [[{"navigate": {"url": "https://evil.example/collect"}}]], subgoal=Subgoal(id=1, goal="read", success_criterion="x"))
    assert [e for e in events if e[0] == "capability_denied"][0][1]["reason"] == "navigation outside the allowed sites"
    assert res.status == "failed" and FakeAgent.last.stopped


async def test_the_same_action_is_not_checked_twice(run):
    typed = {"input_text": {"index": 4, "text": PH_PW}}
    async def behaviour(agent, hooks):
        agent.history = ActionHistory()
        _step(agent, typed)
        await hooks[1](agent)
        await hooks[1](agent)           # a second hook call with no new action must not count the password again
        await hooks[1](agent)
        return agent.history
    FakeAgent.behaviour = staticmethod(behaviour)
    res, events = await run(subgoal=AUTH)
    assert not [e for e in events if e[0] == "capability_denied"]


class ResultHistory(ActionHistory):
    """Also reports one result per action, so a typing that did not happen (no such field on the page) can be told apart."""
    def __init__(self, failing: set[int]):
        super().__init__()
        self.failing = failing

    def action_results(self):
        return [SimpleNamespace(error="Element index 4 not found" if i in self.failing else None) for i in range(len(self.acts))]


async def test_typing_that_failed_does_not_count_toward_the_password_cap(run):
    """Found live: after a good sign-in the agent kept 'typing' into a page with no password field; three tries tripped the cap
    and a correct login was reported as rejected."""
    typed = {"input_text": {"index": 4, "text": PH_PW}}

    async def behaviour(agent, hooks):
        agent.history = ResultHistory(failing={1, 2})
        for _ in range(3):
            _step(agent, typed)
            await hooks[1](agent)
        return agent.history
    FakeAgent.behaviour = staticmethod(behaviour)
    res, events = await run(subgoal=AUTH)
    assert not [e for e in events if e[0] == "capability_denied"] and not FakeAgent.last.stopped


async def test_three_successful_password_typings_are_still_refused(run):
    typed = {"input_text": {"index": 4, "text": PH_PW}}

    async def behaviour(agent, hooks):
        agent.history = ResultHistory(failing=set())
        for _ in range(3):
            _step(agent, typed)
            await hooks[1](agent)
        return agent.history
    FakeAgent.behaviour = staticmethod(behaviour)
    res, events = await run(subgoal=AUTH)
    assert [e for e in events if e[0] == "capability_denied"] and FakeAgent.last.stopped


def test_step_lines_are_short_single_line_and_free_of_element_numbers():
    from app.browser.executor import STEP_LINE_CHARS, brief_step
    assert brief_step("\nClick the reorder button [38] to add items,\nthen wait.\n") == "Click the reorder button to add items, then wait."
    assert brief_step("Click the Reorder button (index 108) now") == "Click the Reorder button now"
    long = brief_step("word " * 100)
    assert len(long) <= STEP_LINE_CHARS + 1 and long.endswith("…") and "\n" not in long
    assert brief_step("") == ""


async def test_a_repeated_thought_is_shown_once(run):
    async def behaviour(agent, hooks):
        agent.history = ActionHistory()
        for _ in range(3):
            await hooks[1](agent)
        return agent.history
    FakeAgent.behaviour = staticmethod(behaviour)
    res, events = await run(subgoal=AUTH)
    assert len([e for e in events if e[0] == "step"]) == 1


async def test_only_the_irreversible_step_may_submit_the_final_action(run):
    """Found live: a reversible step approved at the checkout page clicked Place order on its own."""
    await run(subgoal=Subgoal(id=1, goal="review the checkout", success_criterion="checkout shown"))
    assert "must NOT place an order" in FakeAgent.last.kw["extend_system_message"]
    await run(subgoal=Subgoal(id=2, goal="place the order", success_criterion="confirmation", irreversible=True))
    assert "must NOT place an order" not in FakeAgent.last.kw["extend_system_message"]
