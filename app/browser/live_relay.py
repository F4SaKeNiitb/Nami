"""In-page live browser: stream a browser's screen into the Nami page and send the user's clicks and keys back.

Used for takeover sign-ins when the browser host has no live view of its own (the local provider). With it the login
browser runs headless on the server and the user signs in inside the chat page; no window pops up anywhere.

Boundaries:
  * a viewer needs a ticket that is random (256 bits), single-task, short-lived and bound to one user. The WebSocket also
    checks the Chainlit session cookie, so a leaked ticket alone is useless;
  * only screen frames go out and only input events come in; the relay never reads page text, cookies or storage;
  * the model is not running while the user is in control, and nothing the relay sees is logged or stored;
  * revoking a task's tickets (hand-back, cancel, finish) closes any open viewer at once.
"""

import asyncio
import base64
import json
import logging
import secrets
import time
from dataclasses import dataclass, field

log = logging.getLogger("nami.live")

TICKET_TTL = 30 * 60            # seconds a viewer link stays valid
MAX_W, MAX_H = 1280, 800        # screencast size cap
_KEYS = {                       # special keys the page needs as raw key events (others arrive as typed text)
    "Enter": 13, "Backspace": 8, "Tab": 9, "Escape": 27, "Delete": 46, "ArrowLeft": 37, "ArrowUp": 38,
    "ArrowRight": 39, "ArrowDown": 40, "Home": 36, "End": 35, "PageUp": 33, "PageDown": 34,
}
_BUTTONS = {0: "left", 1: "middle", 2: "right"}


@dataclass
class Ticket:
    user_id: str
    task_id: str
    cdp_url: str
    expires: float
    viewers: set = field(default_factory=set)        # open WebSocket connections, closed on revoke


class LiveRelay:
    def __init__(self, ttl: float = TICKET_TTL):
        self.ttl = ttl
        self._tickets: dict[str, Ticket] = {}

    def issue(self, *, user_id: str, task_id: str, cdp_url: str) -> str:
        self._sweep()
        token = secrets.token_urlsafe(32)
        self._tickets[token] = Ticket(user_id, task_id, cdp_url, time.monotonic() + self.ttl)
        return token

    def check(self, token: str, user_id: str | None) -> Ticket | None:
        """The ticket if it exists, is unexpired and belongs to this signed-in user; otherwise None."""
        self._sweep()
        t = self._tickets.get(token or "")
        if t is None or not user_id or not secrets.compare_digest(t.user_id, user_id):
            return None
        return t

    async def revoke_task(self, task_id: str) -> None:
        for token, t in list(self._tickets.items()):
            if t.task_id == task_id:
                self._tickets.pop(token, None)
                for ws in list(t.viewers):
                    try:
                        await ws.close(code=4001)
                    except Exception:
                        pass

    def _sweep(self) -> None:
        now = time.monotonic()
        for token, t in list(self._tickets.items()):
            if t.expires < now:
                self._tickets.pop(token, None)

    async def serve(self, ws, ticket: Ticket) -> None:
        """Pump frames out and input in until the viewer leaves or the ticket is revoked."""
        from app.browser import cdp
        ticket.viewers.add(ws)
        try:
            async with cdp.connect(ticket.cdp_url) as browser:
                ctx = browser.contexts[0]
                view = _View(ws)
                await view.follow(ctx.pages[-1] if ctx.pages else await ctx.new_page(), ctx)
                ctx.on("page", lambda p: asyncio.create_task(view.follow(p, ctx)))     # sign-in popups open new tabs
                while True:
                    raw = await ws.receive_text()
                    if len(raw) > 20_000:
                        continue
                    try:
                        await view.input(json.loads(raw))
                    except Exception:
                        log.debug("bad input event dropped", exc_info=True)
        except Exception as e:
            if type(e).__name__ not in ("WebSocketDisconnect", "ConnectionClosed"):
                log.info("live view ended: %s", type(e).__name__)
        finally:
            ticket.viewers.discard(ws)


class _View:
    """One viewer of one browser: streams the most recent tab and translates input into CDP Input events."""

    def __init__(self, ws):
        self.ws, self.page, self.session = ws, None, None
        self._lock = asyncio.Lock()

    async def follow(self, page, ctx) -> None:
        async with self._lock:
            if self.session is not None:
                try:
                    await self.session.send("Page.stopScreencast")
                    await self.session.detach()
                except Exception:
                    pass
            self.page = page
            self.session = await ctx.new_cdp_session(page)
            self.session.on("Page.screencastFrame", self._frame)
            page.on("close", lambda _p: asyncio.create_task(self._fallback(ctx)))
            await self.session.send("Page.startScreencast", {"format": "jpeg", "quality": 60,
                                                              "maxWidth": MAX_W, "maxHeight": MAX_H, "everyNthFrame": 1})

    async def _fallback(self, ctx) -> None:
        if ctx.pages:
            await self.follow(ctx.pages[-1], ctx)

    async def _frame(self, ev: dict) -> None:
        meta = ev.get("metadata", {})
        try:
            await self.session.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]})
            await self.ws.send_text(json.dumps({"t": "frame", "data": ev["data"],
                                                "w": meta.get("deviceWidth"), "h": meta.get("deviceHeight"),
                                                "url": _host(self.page.url if self.page else "")}))
        except Exception:
            pass                       # viewer gone; serve() notices on the next receive

    async def input(self, m: dict) -> None:
        s, kind = self.session, m.get("t")
        if s is None:
            return
        if kind in ("down", "up", "move"):
            x, y = float(m.get("x", 0)), float(m.get("y", 0))
            typ = {"down": "mousePressed", "up": "mouseReleased", "move": "mouseMoved"}[kind]
            await s.send("Input.dispatchMouseEvent", {"type": typ, "x": x, "y": y, "button": _BUTTONS.get(int(m.get("b", 0)), "left")
                                                      if kind != "move" else "none", "clickCount": 1 if kind != "move" else 0})
        elif kind == "wheel":
            await s.send("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": float(m.get("x", 0)), "y": float(m.get("y", 0)),
                                                      "deltaX": float(m.get("dx", 0)), "deltaY": float(m.get("dy", 0))})
        elif kind == "text":
            text = str(m.get("text", ""))[:2000]
            if text:
                await s.send("Input.insertText", {"text": text})
        elif kind == "key":
            key = str(m.get("key", ""))
            if key in _KEYS:
                code = _KEYS[key]
                down = {"type": "keyDown", "text": "\r"} if key == "Enter" else {"type": "rawKeyDown"}   # Enter must type "\r" to submit
                for ev in (down, {"type": "keyUp"}):
                    await s.send("Input.dispatchKeyEvent", {**ev, "key": key, "code": key,
                                                            "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code})
        elif kind == "back" and self.page is not None:
            await self.page.go_back()


def _host(url: str) -> str:
    """Only the host is shown to the viewer (a path can carry tokens)."""
    try:
        from urllib.parse import urlparse
        return urlparse(url).hostname or ""
    except Exception:
        return ""


def frame_to_bytes(data: str) -> bytes:          # used by tests
    return base64.b64decode(data)
