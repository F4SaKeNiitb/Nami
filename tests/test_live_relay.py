"""In-page live browser (sign-in inside the chat page instead of a window on the server).

Safety first: a viewer needs a ticket AND the same signed-in user; tickets expire and die with the task; the relay only
streams frames and sends input (it never reads page content); the viewer sees the host, never a URL path."""
import json
import stat
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.browser import live_relay as lr
from app.browser.live_relay import LiveRelay, _View
from app.browser.provider import BrowserSession


# ── tickets ──

def test_a_ticket_only_works_for_the_user_it_was_issued_to():
    relay = LiveRelay()
    t = relay.issue(user_id="u1", task_id="t1", cdp_url="http://127.0.0.1:9")
    assert relay.check(t, "u1") is not None
    assert relay.check(t, "u2") is None
    assert relay.check(t, None) is None              # no session cookie
    assert relay.check("guessed-ticket", "u1") is None


def test_tickets_are_random_and_long():
    relay = LiveRelay()
    a, b = (relay.issue(user_id="u", task_id="t", cdp_url="x") for _ in range(2))
    assert a != b and len(a) >= 40


def test_tickets_expire(monkeypatch):
    relay = LiveRelay(ttl=10)
    now = [1000.0]
    monkeypatch.setattr(lr.time, "monotonic", lambda: now[0])
    t = relay.issue(user_id="u1", task_id="t1", cdp_url="x")
    now[0] += 11
    assert relay.check(t, "u1") is None


class FakeWS:
    def __init__(self):
        self.closed, self.sent = None, []

    async def close(self, code=1000):
        self.closed = code

    async def send_text(self, text):
        self.sent.append(json.loads(text))


async def test_revoking_a_task_kills_its_tickets_and_closes_open_viewers():
    relay = LiveRelay()
    t = relay.issue(user_id="u1", task_id="t1", cdp_url="x")
    other = relay.issue(user_id="u1", task_id="t2", cdp_url="x")
    ws = FakeWS()
    relay.check(t, "u1").viewers.add(ws)
    await relay.revoke_task("t1")
    assert relay.check(t, "u1") is None and ws.closed == 4001
    assert relay.check(other, "u1") is not None      # other tasks untouched


# ── the orchestrator only hands tickets to the task's own user, and only when needed ──

class _Orch:
    """Just enough of the Orchestrator to exercise live_ticket()."""
    from app.agent.graph import Orchestrator as _O
    live_ticket = _O.live_ticket
    _in_page = _O._in_page

    def __init__(self, view="inpage", live_view_url=None):
        from types import SimpleNamespace
        self.s = SimpleNamespace(takeover_view=view)
        self.live_relay = LiveRelay()
        self._live = {"t1": BrowserSession("s1", "u1", "http://127.0.0.1:9", live_view_url)}


def test_live_ticket_refuses_another_user_and_unknown_tasks():
    o = _Orch()
    assert o.live_ticket("t1", "u2") is None
    assert o.live_ticket("nope", "u1") is None
    assert o.live_ticket("t1", "u1")


def test_no_relay_when_the_host_has_its_own_live_view_or_window_mode_is_chosen():
    assert _Orch(live_view_url="https://kernel/live/x").live_ticket("t1", "u1") is None
    assert _Orch(view="window").live_ticket("t1", "u1") is None


# ── the WebSocket route ──

@pytest.fixture
def live_app(monkeypatch):
    from types import SimpleNamespace
    from app.browser import live_router
    relay = LiveRelay()
    served = []

    async def serve(ws, ticket):
        served.append(ticket.user_id)
        await ws.send_text("{}")
    relay.serve = serve
    monkeypatch.setattr(live_router, "runtime", lambda: SimpleNamespace(live_relay=relay))
    app = FastAPI()
    app.include_router(live_router.router)
    return app, relay, served, live_router


def test_websocket_without_the_owners_session_is_closed_before_any_frame(live_app, monkeypatch):
    app, relay, served, router = live_app
    t = relay.issue(user_id="u1", task_id="t1", cdp_url="x")
    monkeypatch.setattr(router, "user_id_from_cookies", lambda c: None)
    with pytest.raises(WebSocketDisconnect):
        with TestClient(app).websocket_connect(f"/chainlit/live/ws/{t}") as ws:
            ws.receive_text()
    monkeypatch.setattr(router, "user_id_from_cookies", lambda c: "u2")
    with pytest.raises(WebSocketDisconnect):
        with TestClient(app).websocket_connect(f"/chainlit/live/ws/{t}") as ws:
            ws.receive_text()
    assert served == []


def test_websocket_from_another_site_is_refused(live_app, monkeypatch):
    app, relay, served, router = live_app
    t = relay.issue(user_id="u1", task_id="t1", cdp_url="x")
    monkeypatch.setattr(router, "user_id_from_cookies", lambda c: "u1")
    with pytest.raises(WebSocketDisconnect):
        with TestClient(app).websocket_connect(f"/chainlit/live/ws/{t}", headers={"origin": "https://evil.example"}) as ws:
            ws.receive_text()
    assert served == []


def test_owner_with_ticket_is_served(live_app, monkeypatch):
    app, relay, served, router = live_app
    t = relay.issue(user_id="u1", task_id="t1", cdp_url="x")
    monkeypatch.setattr(router, "user_id_from_cookies", lambda c: "u1")
    with TestClient(app).websocket_connect(f"/chainlit/live/ws/{t}") as ws:
        assert ws.receive_text() == "{}"
    assert served == ["u1"]


def test_a_forged_session_cookie_is_not_accepted():
    from app.browser.live_router import user_id_from_cookies
    assert user_id_from_cookies({"access_token": "not.a.jwt"}) is None
    assert user_id_from_cookies({}) is None


# ── what the relay does with the browser ──

class FakeSession:
    def __init__(self):
        self.calls = []

    async def send(self, method, params=None):
        self.calls.append((method, params or {}))


ALLOWED = {"Input.dispatchMouseEvent", "Input.dispatchKeyEvent", "Input.insertText",
           "Page.startScreencast", "Page.stopScreencast", "Page.screencastFrameAck"}


async def test_input_becomes_cdp_input_events_only():
    v = _View(FakeWS())
    v.session = FakeSession()
    for m in [{"t": "down", "x": 10, "y": 20, "b": 0}, {"t": "up", "x": 10, "y": 20}, {"t": "move", "x": 1, "y": 2},
              {"t": "wheel", "x": 1, "y": 1, "dy": 120}, {"t": "text", "text": "hello"}, {"t": "key", "key": "Enter"},
              {"t": "key", "key": "F12"}, {"t": "eval", "js": "document.cookie"}, {"t": "text", "text": ""}]:
        await v.input(m)
    methods = [c[0] for c in v.session.calls]
    assert set(methods) <= ALLOWED                    # nothing that reads the page or runs script
    assert ("Input.insertText", {"text": "hello"}) in v.session.calls
    enter = [p for m, p in v.session.calls if m == "Input.dispatchKeyEvent" and p["type"] == "keyDown"]
    assert enter and enter[0]["text"] == "\r"         # Enter submits the form
    assert not any(p.get("key") == "F12" for _, p in v.session.calls)    # unknown keys (devtools) are dropped


async def test_typed_text_is_capped():
    v = _View(FakeWS())
    v.session = FakeSession()
    await v.input({"t": "text", "text": "x" * 10_000})
    assert len(v.session.calls[0][1]["text"]) == 2000


async def test_frames_show_only_the_host_not_the_url_path():
    ws = FakeWS()
    v = _View(ws)
    v.session = FakeSession()

    class P:
        url = "https://accounts.google.com/signin/v2?token=SECRET123&continue=x"
    v.page = P()
    await v._frame({"data": "aGk=", "sessionId": 7, "metadata": {"deviceWidth": 1280, "deviceHeight": 800}})
    assert ws.sent[0]["url"] == "accounts.google.com"
    assert "SECRET123" not in json.dumps(ws.sent)
    assert ("Page.screencastFrameAck", {"sessionId": 7}) in v.session.calls


# ── headless Chrome presents as normal Chrome ──

@pytest.mark.skipif(sys.platform == "win32", reason="uses a shell script as a fake chrome")
def test_headless_flags_drop_headless_from_the_user_agent(tmp_path):
    from app.browser.local_provider import _UA_CACHE, headless_flags
    fake = tmp_path / "chrome"
    fake.write_text("#!/bin/sh\necho 'Google Chrome 141.0.7390.54'\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    _UA_CACHE.clear()
    flags = headless_flags(str(fake))
    ua = next(f for f in flags if f.startswith("--user-agent="))
    assert "--headless=new" in flags and "Chrome/141.0.0.0" in ua and "Headless" not in ua


def test_headless_flags_without_a_readable_version_still_run_headless(tmp_path):
    from app.browser.local_provider import _UA_CACHE, headless_flags
    _UA_CACHE.clear()
    flags = headless_flags(str(tmp_path / "missing-chrome"))
    assert "--headless=new" in flags and not any(f.startswith("--user-agent=") for f in flags)
