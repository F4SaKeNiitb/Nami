"""Browser Use Cloud hosted browsers, with a saved profile per user.

REST, API v4 (https://docs.browser-use.com/cloud/api-v4/): `POST /browsers` returns `cdpUrl` and `liveUrl`; `PATCH /browsers/{id}
{"action":"stop"}` ends it (a closed CDP connection does NOT stop the billing, so `_close` must always reach it). A profile keeps
cookies and local storage between browsers; Nami's own encrypted per-site snapshots are restored on top of it either way.

Not verified against the live service from here (no key was available when this was written): the profile-search response shape
(`GET /profiles?userId=`), and whether the profile is written back when a browser is stopped. Both fail soft: a missing profile
means a browser without one, and the snapshots still bring the logins back.
"""

import hashlib
import logging
import math

import httpx

from app.browser.provider import BrowserProvider, BrowserSession

log = logging.getLogger("nami.browser_use")

BASE = "https://api.browser-use.com/api/v4"


class BrowserUseError(Exception):
    pass


class BrowserUseProvider(BrowserProvider):
    def __init__(self, *, api_key: str, timeout_seconds: int = 900, proxy_country: str | None = "in",
                 client: httpx.AsyncClient | None = None, base_url: str = BASE):
        super().__init__()
        self.client = client or httpx.AsyncClient(timeout=30.0)
        self.base = base_url.rstrip("/")
        self.headers = {"X-Browser-Use-API-Key": api_key, "Content-Type": "application/json"}
        self.timeout_minutes = min(240, max(1, math.ceil(timeout_seconds / 60)))
        self.proxy_country = proxy_country            # None turns the vendor's proxy off; the default (India) keeps Indian sites on a local address
        self._profiles: dict[str, str] = {}           # user_id -> profile id

    @staticmethod
    def owner_tag(user_id: str) -> str:
        return "nami-" + hashlib.sha256(user_id.encode()).hexdigest()[:24]       # no raw ids/emails at the vendor

    async def _call(self, method: str, path: str, **kw) -> dict:
        try:
            r = await self.client.request(method, f"{self.base}{path}", headers=self.headers, **kw)
        except httpx.HTTPError as e:
            raise BrowserUseError(f"could not reach Browser Use: {type(e).__name__}") from None
        if r.status_code == 429:
            raise BrowserUseError("Browser Use says there are too many browsers open at once for this plan.")
        if r.status_code == 402:
            raise BrowserUseError("Browser Use is out of credits or at its spend limit.")
        if not r.is_success:
            raise BrowserUseError(f"Browser Use returned HTTP {r.status_code} for {method} {path}")
        return r.json() if r.content else {}

    async def _profile_id(self, user_id: str) -> str | None:
        if user_id in self._profiles:
            return self._profiles[user_id]
        tag = self.owner_tag(user_id)
        try:
            found = await self._call("GET", "/profiles", params={"userId": tag})
            items = found if isinstance(found, list) else found.get("items") or found.get("profiles") or []
            pid = next((i["id"] for i in items if i.get("userId") == tag), None)
            if pid is None:
                pid = (await self._call("POST", "/profiles", json={"name": tag, "userId": tag}))["id"]
        except (BrowserUseError, KeyError, TypeError):
            log.warning("no Browser Use profile for this user; the browser starts without one", exc_info=False)
            return None
        self._profiles[user_id] = pid
        return pid

    async def _open(self, user_id: str, *, headless: bool, scratch: bool = False) -> BrowserSession:
        body: dict = {"timeout": self.timeout_minutes, "proxyCountryCode": self.proxy_country}
        if not scratch and (pid := await self._profile_id(user_id)):
            body["profileId"] = pid
        b = await self._call("POST", "/browsers", json=body)
        if not b.get("cdpUrl"):
            raise BrowserUseError("Browser Use did not return a browser address")
        return BrowserSession(b["id"], user_id, b["cdpUrl"], b.get("liveUrl"))

    async def _close(self, session: BrowserSession) -> None:
        try:
            await self._call("PATCH", f"/browsers/{session.session_id}", json={"action": "stop"})
        except BrowserUseError as e:
            log.warning("could not stop browser %s: %s", session.session_id, e)      # it times out on its own; never block the cleanup
