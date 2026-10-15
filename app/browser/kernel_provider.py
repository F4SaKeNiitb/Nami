"""Kernel hosted browsers with a persistent Profile per user.

Verified against kernel SDK 0.119: `browsers.create(profile={"name", "save_changes"}, headless,
stealth, timeout_seconds)` returns `cdp_ws_url`, `browser_live_view_url`, `session_id`.
`browsers.delete_by_id` is what writes the profile back, so `_close` must always reach it.
"""

import hashlib

import logging

from kernel import AsyncKernel, BadRequestError, PermissionDeniedError

from app.browser.provider import BrowserProvider, BrowserSession

log = logging.getLogger("nami.kernel")


class KernelProvider(BrowserProvider):
    def __init__(self, client: AsyncKernel | None = None, *, api_key: str | None = None,
                 timeout_seconds: int = 600, stealth: bool = True, headless: bool = True, region: str | None = None):
        super().__init__()
        self.region = region              # us-east | eu-west | ap-southeast; None lets Kernel choose
        self.headless = headless          # what every session is, whatever the caller asks for. Default headless (see Settings.kernel_headless); it runs in Kernel's cloud, so no window opens here either way
        self.client = client or AsyncKernel(api_key=api_key)
        self.timeout_seconds = timeout_seconds
        self.stealth = stealth

    @staticmethod
    def profile_name(user_id: str) -> str:
        return "nami-" + hashlib.sha256(user_id.encode()).hexdigest()[:24]   # no raw ids/emails at the vendor

    async def _create(self, user_id: str, headless: bool, scratch: bool = False):
        args = dict(headless=self.headless, stealth=self.stealth, timeout_seconds=self.timeout_seconds)
        if not scratch:
            args["profile"] = {"name": self.profile_name(user_id), "save_changes": True}
        if self.region:
            try:
                return await self.client.browsers.create(region=self.region, **args)
            except PermissionDeniedError as e:            # choosing a region is a paid-plan feature: say so once, then carry on without it
                if "insufficient_plan" not in str(e):
                    raise
                log.warning("Kernel region %r needs a paid plan; using Kernel's default region instead", self.region)
                self.region = None
        return await self.client.browsers.create(**args)

    async def _open(self, user_id: str, *, headless: bool, scratch: bool = False) -> BrowserSession:
        try:
            b = await self._create(user_id, headless, scratch)
        except BadRequestError as e:                  # Kernel only attaches a profile that already exists: first use creates it
            if "profile not found" not in str(e).lower():
                raise
            await self.client.profiles.create(name=self.profile_name(user_id))
            b = await self._create(user_id, headless)
        return BrowserSession(b.session_id, user_id, b.cdp_ws_url, b.browser_live_view_url)

    async def _close(self, session: BrowserSession) -> None:
        await self.client.browsers.delete_by_id(session.session_id)
