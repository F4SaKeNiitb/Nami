"""Keeps logins alive: encrypted per-site snapshots, restore before a task, capture after, revoke on request."""

import logging
import time

from sqlalchemy import select

from app.browser.provider import BrowserProvider, ProfileBusy
from app.browser.snapshots import SiteSnapshot, clear_site, restore_site, snapshot_site
from app.db.base import utcnow
from app.db.engine import Database
from app.db.models import BrowserSnapshot
from app.sites.loader import SiteHints, looks_logged_out
from app.vault.crypto import DecryptionError, Sealed
from app.vault.service import VaultService


log = logging.getLogger(__name__)


def _aad(user_id: str, site: str) -> bytes:
    return f"snapshot|{user_id}|{site}".encode()


class SessionManager:
    def __init__(self, db: Database, vault: VaultService, *, health_ttl: float = 300.0, provider: BrowserProvider | None = None):
        self.db, self.vault = db, vault
        self.provider = provider           # lets a revoke clear the site's cookies from the saved browser profile, not just forget the snapshot
        self.health_ttl = health_ttl
        self._healthy: dict[tuple[str, str], float] = {}      # (user, site) -> when a passing check expires; memory only

    def health_cached(self, user_id: str, site: str) -> bool:
        """True if this user's session on the site passed the sign-in check a moment ago (a failed check is never cached)."""
        until = self._healthy.get((user_id, site))
        if until is not None and until > time.monotonic():
            return True
        self._healthy.pop((user_id, site), None)
        return False

    def remember_health(self, user_id: str, site: str, ok: bool) -> None:
        if ok and self.health_ttl > 0:
            self._healthy[(user_id, site)] = time.monotonic() + self.health_ttl
        else:
            self._healthy.pop((user_id, site), None)

    async def check(self, user_id: str, site: str, cdp_url: str, hints: SiteHints) -> bool:
        """The sign-in check with the short cache. A check that could not run (the page timed out, the browser dropped) lets the
        task go on, because the agent will notice a login page itself, but it is never remembered as a pass."""
        if self.health_cached(user_id, site):
            return True
        try:
            ok = await self.is_logged_in(cdp_url, hints)
        except Exception:
            log.warning("health check failed", exc_info=True)
            self.remember_health(user_id, site, False)
            return True
        self.remember_health(user_id, site, ok)
        return ok

    async def sites(self, user_id: str) -> set[str]:
        async with self.db.session() as s:
            rows = await s.scalars(select(BrowserSnapshot.site).where(BrowserSnapshot.user_id == user_id))
            return set(rows.all())

    async def capture(self, user_id: str, cdp_url: str, site: str) -> bool:
        """Save the site's cookies + localStorage, encrypted. No-op (False) if there is nothing to save."""
        snap = await snapshot_site(cdp_url, site)
        if not snap.cookies and not snap.local_storage:
            return False
        import json
        sealed = await self.vault.seal_for_user(user_id, {"snapshot": json.dumps(snap.to_dict())}, _aad(user_id, site))
        async with self.db.session() as s:
            row = await s.scalar(select(BrowserSnapshot).where(BrowserSnapshot.user_id == user_id,
                                                               BrowserSnapshot.site == site))
            if row is None:
                s.add(BrowserSnapshot(user_id=user_id, site=site, ciphertext=sealed.ciphertext, nonce=sealed.nonce))
            else:
                row.ciphertext, row.nonce, row.captured_at = sealed.ciphertext, sealed.nonce, utcnow()
        return True

    async def restore(self, user_id: str, cdp_url: str, site: str) -> bool:
        async with self.db.session() as s:
            row = await s.scalar(select(BrowserSnapshot).where(BrowserSnapshot.user_id == user_id,
                                                               BrowserSnapshot.site == site))
            blob = None if row is None else Sealed(row.ciphertext, row.nonce)
        if blob is None:
            return False
        import json
        try:
            data = await self.vault.open_for_user(user_id, blob, _aad(user_id, site))
        except DecryptionError:
            return False
        await restore_site(cdp_url, SiteSnapshot.from_dict(json.loads(data["snapshot"])))
        return True

    async def revoke(self, user_id: str, site: str, cdp_url: str | None = None) -> bool:
        """Sign out of a site for good: delete the stored snapshot AND clear the site's cookies and localStorage from the user's
        browser profile, which is what actually keeps them signed in between tasks. With no browser open, a short headless one is
        opened on the profile just for this. Returns True when the profile was cleared; False when it could not be (the profile
        is in use by a running task, or there is no provider), so the caller can say so instead of claiming a sign-out."""
        self._healthy.pop((user_id, site), None)
        cleared = False
        try:
            if cdp_url:
                await clear_site(cdp_url, site)
                cleared = True
            elif self.provider is not None:
                try:
                    session = await self.provider.open(user_id, headless=True)
                except ProfileBusy:
                    session = None
                if session is not None:
                    try:
                        await clear_site(session.cdp_url, site)
                        cleared = True
                    finally:
                        await self.provider.close(session)
        except Exception:
            log.warning("clearing the browser profile failed", exc_info=False)
        async with self.db.session() as s:
            row = await s.scalar(select(BrowserSnapshot).where(BrowserSnapshot.user_id == user_id,
                                                               BrowserSnapshot.site == site))
            if row is not None:
                await s.delete(row)
        return cleared

    async def is_logged_in(self, cdp_url: str, hints: SiteHints) -> bool:
        """Health check: open the site's auth-check page and see whether it bounced to a login page."""
        if not hints.auth_check_url:
            return True
        from app.browser import cdp
        async with cdp.connect(cdp_url) as browser:
            page = await browser.contexts[0].new_page()
            try:
                await page.goto(hints.auth_check_url, wait_until="domcontentloaded", timeout=20000)
                try:
                    await page.wait_for_load_state("load", timeout=4000)      # let a client-side redirect to the sign-in page happen
                except Exception:
                    pass
                has_password_box = await page.locator("input[type=password]").count() > 0
                on_expected_page = not hints.expect_url or hints.expect_url in page.url
                from app.browser.login_probe import SCAN_JS, page_shows_a_login
                try:                                   # phone-and-code sites have no password box anywhere: look at the whole form, not one input type
                    data = await page.evaluate(SCAN_JS)
                    shows_login = page_shows_a_login(data["inputs"], data["buttons"])
                except Exception:
                    shows_login = False
                return on_expected_page and not looks_logged_out(page.url, hints) and not has_password_box and not shows_login
            finally:
                await page.close()
