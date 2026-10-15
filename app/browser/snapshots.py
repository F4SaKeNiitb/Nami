"""Per-site session snapshots over plain CDP, so they work on any provider.

A snapshot holds cookies and localStorage for ONE registrable domain. It is a secret
(a session cookie is a password for as long as it lives): callers must encrypt it before
it touches disk (Session Manager, step 2.4).
"""

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.browser import cdp

from app.vault.origins import registrable_domain


@dataclass
class SiteSnapshot:
    site: str
    cookies: list[dict] = field(default_factory=list)
    local_storage: dict[str, dict[str, str]] = field(default_factory=dict)   # origin -> {key: value}

    def __repr__(self) -> str:
        return f"SiteSnapshot(site={self.site!r}, cookies={len(self.cookies)}, origins={len(self.local_storage)})"

    def to_dict(self) -> dict:
        return {"site": self.site, "cookies": self.cookies, "local_storage": self.local_storage}

    @classmethod
    def from_dict(cls, d: dict) -> "SiteSnapshot":
        return cls(d["site"], d["cookies"], d["local_storage"])


def _cookie_site(c: dict) -> str | None:
    return registrable_domain(c["domain"].lstrip("."))


async def _with_context(cdp_url: str, fn):
    async with cdp.connect(cdp_url) as browser:        # disconnects on exit; the remote browser stays open
        return await fn(browser.contexts[0])


def _candidate_origins(site: str, cookies: list[dict], pages, extra: list[str] | None) -> set[str]:
    """Origins whose localStorage may hold session state. CDP only reports origins with an open
    page, so we list the likely ones and visit them (network stubbed) to read their storage."""
    found = {f"https://{site}", f"https://www.{site}"}
    for c in cookies:
        host = c["domain"].lstrip(".")
        found.add(f"{'https' if c.get('secure') else 'http'}://{host}")
        found.add(f"https://{host}")
    for page in pages:
        if page.url.startswith(("http://", "https://")):
            parts = urlsplit(page.url)
            found.add(f"{parts.scheme}://{parts.netloc}")
    for o in extra or []:
        found.add(o.rstrip("/"))
    return {o for o in found if registrable_domain(o) == site}


async def _stubbed_page(ctx, origin: str):
    page = await ctx.new_page()
    await page.route("**/*", lambda r: r.fulfill(body="<html></html>", content_type="text/html"))
    await page.goto(origin + "/")
    return page


async def snapshot_site(cdp_url: str, site: str, *, origins: list[str] | None = None) -> SiteSnapshot:
    """`origins`: extra origins to read localStorage from (e.g. the site's auth-check URL origin)."""
    site = registrable_domain(site) or ""
    if not site:
        raise ValueError("invalid site")

    async def run(ctx):
        cookies = [c for c in await ctx.cookies() if _cookie_site(c) == site]
        ls: dict[str, dict[str, str]] = {}
        for origin in sorted(_candidate_origins(site, cookies, ctx.pages, origins)):
            page = await _stubbed_page(ctx, origin)
            try:
                items = await page.evaluate("() => Object.fromEntries(Object.entries(localStorage))")
            finally:
                await page.close()
            if items:
                ls[origin] = items
        return SiteSnapshot(site, cookies, ls)

    return await _with_context(cdp_url, run)


async def restore_site(cdp_url: str, snap: SiteSnapshot) -> None:
    """Restore only entries that belong to snap.site, even if the snapshot was tampered with."""
    async def run(ctx):
        ok = [c for c in snap.cookies if _cookie_site(c) == snap.site]
        if ok:
            await ctx.add_cookies(ok)
        for origin, items in snap.local_storage.items():
            if registrable_domain(origin) != snap.site or not items:
                continue
            page = await _stubbed_page(ctx, origin)
            try:
                await page.evaluate("items => { for (const [k, v] of Object.entries(items)) localStorage.setItem(k, v) }", items)
            finally:
                await page.close()

    await _with_context(cdp_url, run)


async def clear_site(cdp_url: str, site: str, *, origins: list[str] | None = None) -> None:
    """Revocation: remove every cookie and localStorage entry of one site, nothing else."""
    site = registrable_domain(site) or ""
    if not site:
        raise ValueError("invalid site")

    async def run(ctx):
        cookies = [c for c in await ctx.cookies() if _cookie_site(c) == site]
        for c in cookies:
            await ctx.clear_cookies(name=c["name"], domain=c["domain"], path=c["path"])
        for origin in sorted(_candidate_origins(site, cookies, ctx.pages, origins)):
            page = await _stubbed_page(ctx, origin)
            try:
                await page.evaluate("localStorage.clear()")
            finally:
                await page.close()

    await _with_context(cdp_url, run)
