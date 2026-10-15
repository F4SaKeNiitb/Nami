"""One shared Playwright driver per event loop, and short-lived CDP connections on top of it.

Starting a Playwright driver spawns a Node process; doing that for every snapshot, health check and
page read was slow and left many driver processes to tear down at exit.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from playwright.async_api import Browser, Playwright, async_playwright

log = logging.getLogger("nami.cdp")
_state: dict = {"loop": None, "pw": None, "lock": None}


async def _playwright() -> Playwright:
    loop = asyncio.get_running_loop()
    if _state["loop"] is not loop:                   # a different/closed loop (tests): start over
        _state.update(loop=loop, pw=None, lock=asyncio.Lock())
    async with _state["lock"]:
        if _state["pw"] is None:
            _state["pw"] = await async_playwright().start()
        return _state["pw"]


ATTACH_ATTEMPTS = 4
ATTACH_BACKOFF = 0.5          # seconds, doubled each retry


async def _attach(cdp_url: str) -> Browser:
    """Chrome sometimes refuses the first attach right after a page or sign-in change (seen live: "Browser context
    management is not supported"). It is transient, so retry a few times before giving up."""
    delay = ATTACH_BACKOFF
    for attempt in range(1, ATTACH_ATTEMPTS + 1):
        try:
            return await (await _playwright()).chromium.connect_over_cdp(cdp_url)
        except Exception as e:                       # playwright Error, connection refused, timeout
            if attempt == ATTACH_ATTEMPTS:
                raise
            log.warning("CDP attach failed (attempt %d/%d): %s", attempt, ATTACH_ATTEMPTS, str(e).splitlines()[0][:140])
            await asyncio.sleep(delay)
            delay *= 2


@asynccontextmanager
async def connect(cdp_url: str):
    """Attach to a running browser over CDP; leaving the block disconnects but leaves the browser running."""
    browser: Browser = await _attach(cdp_url)
    try:
        yield browser
    finally:
        await browser.close()


@asynccontextmanager
async def default_context(cdp_url: str):
    """The browser's default context (its normal profile: cookies, storage) over a short CDP attach."""
    async with connect(cdp_url) as browser:
        yield browser.contexts[0]


async def shutdown() -> None:
    pw, _state["pw"] = _state["pw"], None
    if pw is not None and _state["loop"] is asyncio.get_running_loop():
        await pw.stop()
