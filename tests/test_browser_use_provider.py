"""BrowserUseProvider against a fake HTTP server: right requests, profile reuse, scratch has no profile, always stops, no raw ids."""

import json

import httpx
import pytest

from app.browser.browser_use_provider import BrowserUseError, BrowserUseProvider


def make(handler, **kw):
    return BrowserUseProvider(api_key="bu_test_key", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kw)


class Server:
    def __init__(self):
        self.calls, self.profiles, self.n = [], [], 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        self.calls.append((req.method, req.url.path, body, req.headers.get("X-Browser-Use-API-Key")))
        if req.url.path.endswith("/profiles") and req.method == "GET":
            return httpx.Response(200, json={"items": self.profiles})
        if req.url.path.endswith("/profiles"):
            self.profiles.append({"id": "p-1", "userId": body["userId"]})
            return httpx.Response(201, json={"id": "p-1", "userId": body["userId"]})
        if req.method == "POST":
            self.n += 1
            return httpx.Response(201, json={"id": f"b{self.n}", "cdpUrl": f"wss://cdp/{self.n}", "liveUrl": f"https://live/{self.n}"})
        return httpx.Response(200, json={"id": req.url.path.rsplit("/", 1)[1], "status": "stopped"})


async def test_open_creates_a_profile_once_and_attaches_it():
    srv = Server()
    p = make(srv)
    s = await p.open("alice")
    await p.close(s)
    s2 = await p.open("alice")
    creates = [c for c in srv.calls if c[0] == "POST" and c[1].endswith("/browsers")]
    assert [c[2]["profileId"] for c in creates] == ["p-1", "p-1"]
    assert len([c for c in srv.calls if c[1].endswith("/profiles") and c[0] == "POST"]) == 1
    assert s2.cdp_url == "wss://cdp/2" and s2.live_view_url == "https://live/2"


async def test_no_raw_user_id_or_email_goes_to_the_vendor():
    srv = Server()
    await make(srv).open("alice@example.com")
    assert "alice" not in json.dumps([c[2] for c in srv.calls])


async def test_a_scratch_browser_has_no_profile_and_does_not_hold_the_profile():
    srv = Server()
    p = make(srv)
    first = await p.open("alice")
    extra = await p.open("alice", scratch_if_busy=True)
    creates = [c[2] for c in srv.calls if c[0] == "POST" and c[1].endswith("/browsers")]
    assert "profileId" in creates[0] and "profileId" not in creates[1]
    await p.close(extra)
    assert p.is_open("alice")
    await p.close(first)


async def test_close_always_sends_stop_even_if_the_vendor_fails():
    def handler(req):
        if req.method == "PATCH":
            return httpx.Response(500)
        return Server()(req)
    p = make(handler)
    s = await p.open("bob")
    await p.close(s)                                   # does not raise, and releases the profile
    assert not p.is_open("bob")


async def test_timeout_is_in_minutes_and_capped():
    assert make(Server(), timeout_seconds=900).timeout_minutes == 15
    assert make(Server(), timeout_seconds=10 ** 6).timeout_minutes == 240


async def test_a_missing_profile_service_still_gives_a_browser():
    def handler(req):
        if "/profiles" in req.url.path:
            return httpx.Response(500)
        return Server()(req)
    s = await make(handler).open("carol")
    assert s.cdp_url.startswith("wss://")


async def test_too_many_browsers_says_so_plainly():
    p = make(lambda req: httpx.Response(429) if req.url.path.endswith("/browsers") else Server()(req))
    with pytest.raises(BrowserUseError, match="too many browsers"):
        await p.open("dave")
    assert not p.is_open("dave")
