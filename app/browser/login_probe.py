"""Look at a site's real sign-in page and say what it asks for, so Nami can ask the user for exactly that and nothing else.

No per-site knowledge lives here. A short, anonymous browser session opens the site, finds the visible sign-in inputs (if none are
showing it clicks the page's own "Log in" once) and reads only their *type and label*. It never types, never submits and never reads
a field's value. The reading is plain code over those attributes, not a model reading page text, so a hostile page cannot talk
Nami into asking for something else; the worst it can do is make the form ask for the wrong fields, which the user sees.

The result is a LoginShape. An unobserved shape ("couldn't look") makes the caller fall back to a generic, honest form.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass

from app.vault.origins import registrable_domain

log = logging.getLogger("nami.probe")

PROBE_USER = "nami-login-probe"          # one shared anonymous browser profile: public sign-in pages only, never a user's data

_SKIP_INPUT = re.compile(r"search|query|coupon|promo|referral|invite|gift|zip|postal|pin ?code|captcha|newsletter|subscribe", re.I)
_CODE = re.compile(r"\botp\b|one[- ]?time|verification|passcode|security code|(?<![a-z0-9])code(?![a-z0-9])|authenticator", re.I)
_PHONE = re.compile(r"phone|mobile|\btel\b|contact number|\+\d\d|msisdn", re.I)
_EMAIL = re.compile(r"e-?mail", re.I)
_USER = re.compile(r"user ?name|user ?id|login id|member|account", re.I)
_GOOGLE = re.compile(r"google", re.I)
_LOGIN_BUTTON = re.compile(r"^\s*(log ?in|sign ?in|login|sign in / sign up|log in / sign up)\s*$", re.I)

_SCAN = """() => {
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e); return r.width > 4 && r.height > 4 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const t = s => (s || '').toString().trim().slice(0, 80);
  const skip = ['hidden','checkbox','radio','submit','button','image','file','range','color'];
  const inputs = [...document.querySelectorAll('input')].filter(e => !skip.includes(e.type) && vis(e)).slice(0, 25).map(e => ({
    type: e.type || 'text', name: t(e.name), id: t(e.id), placeholder: t(e.placeholder), aria: t(e.getAttribute('aria-label')),
    autocomplete: t(e.autocomplete), label: t(e.labels && e.labels[0] ? e.labels[0].innerText : ''), maxlength: e.maxLength}));
  const buttons = [...document.querySelectorAll('a,button,[role=button],input[type=submit],input[type=button]')].filter(vis).slice(0, 80).map(e => t(e.innerText || e.value || e.getAttribute('aria-label')));
  return {inputs, buttons};
}"""


SCAN_JS = _SCAN                    # shared with the signed-in check in session_manager
LOGIN_BUTTON = _LOGIN_BUTTON
_VERIFY_BUTTON = re.compile(r"verify\s*otp|continue with (phone|mobile)", re.I)


def page_shows_a_login(inputs: list[dict], buttons: list[str]) -> bool:
    """True if the visible page is a sign-in page: a password box, a one-time-code box, or a visible Log in / Sign in / Verify OTP button.
    The old check looked only for a password box, so a phone-and-code site (no password anywhere) always looked signed in."""
    shape = classify(inputs, buttons)
    return bool(shape.password or shape.code or any(_LOGIN_BUTTON.match(b) or _VERIFY_BUTTON.search(b) for b in buttons))


@dataclass(frozen=True)
class LoginShape:
    observed: bool = False                 # False: could not look; every other field is then unknown
    identifier: str | None = None          # phone | email | username | any
    password: bool | None = None           # a password box is on the page
    code: str | None = None                # a one-time-code box is on the page: sms | email | app | code
    code_likely: bool = False              # phone-only sign-in with no password: a texted code almost always follows (inferred, not seen)
    google: bool = False                   # the page offers Google sign-in

    def summary(self, site: str) -> str:
        if not self.observed:
            return f"I couldn't open {site}'s sign-in page just now, so this asks for the usual fields."
        asks = {"phone": "your phone number", "email": "your email", "username": "your username", "any": "your email, phone number or username"}
        parts = [asks[self.identifier]] if self.identifier else []
        if self.password:
            parts.append("your password")
        text = f"{site}'s sign-in page asks for " + (" and ".join(parts) if parts else "a login")
        if self.code or self.code_likely:
            text += ", then a one-time code that I'll ask you for here when it arrives"
        if self.google:
            text += ". It also offers Google sign-in, which I can't automate"
        return text + "."


def _blob(i: dict) -> str:
    return " ".join(str(i.get(k, "")) for k in ("name", "id", "placeholder", "aria", "label", "autocomplete"))


def classify(inputs: list[dict], buttons: list[str]) -> LoginShape:
    """Pure: the visible inputs' attributes and the visible button texts -> what the sign-in asks for."""
    password = any(i.get("type") == "password" for i in inputs)
    code, identifier = None, None
    for i in inputs:
        blob = _blob(i)
        if i.get("type") == "password" or i.get("type") == "search" or _SKIP_INPUT.search(blob):
            continue
        if i.get("autocomplete") == "one-time-code" or _CODE.search(blob):
            if code is None:
                code = "email" if _EMAIL.search(blob) else "sms" if re.search(r"sms|text|mobile|phone", blob, re.I) else "app" if re.search(r"authenticator|app", blob, re.I) else "code"
            continue
        if identifier is not None:
            continue
        ac = str(i.get("autocomplete", "")).lower()
        phone = i.get("type") == "tel" or ac.startswith("tel") or bool(_PHONE.search(blob))
        email = i.get("type") == "email" or ac == "email" or bool(_EMAIL.search(blob))
        user = ac == "username" or bool(_USER.search(blob))
        identifier = "any" if (phone and email) or (email and user) else "phone" if phone else "email" if email else "username" if user else None
        # an unlabelled plain text box (a search field, a newsletter box) is deliberately not taken for a sign-in field
    google = any(_GOOGLE.search(b) for b in buttons)
    observed = bool(identifier or password or code)
    return LoginShape(observed=observed, identifier=identifier, password=password if observed else None, code=code,
                      code_likely=bool(observed and identifier == "phone" and not password and not code), google=google and observed)


class LoginProbe:
    """Opens the site once per few hours (a sign-in page changes rarely) and remembers what it saw, including that it could not look."""

    def __init__(self, provider, *, ttl: float = 6 * 3600, miss_ttl: float = 300, timeout: float = 50, clock=time.monotonic):
        self.provider, self.ttl, self.miss_ttl, self.timeout, self.clock = provider, ttl, miss_ttl, timeout, clock
        self._seen: dict[str, tuple[LoginShape, float]] = {}
        self._lock = asyncio.Lock()                       # one anonymous profile, one probe at a time

    async def shape(self, site: str) -> LoginShape:
        hit = self._seen.get(site)
        if hit and hit[1] > self.clock():
            return hit[0]
        async with self._lock:
            hit = self._seen.get(site)
            if hit and hit[1] > self.clock():
                return hit[0]
            try:
                found = await asyncio.wait_for(self._look(site), self.timeout)
            except Exception as e:                        # a slow, blocked or broken site: say "couldn't look", never fail the task
                log.info("login probe for %s found nothing (%s)", site, type(e).__name__)
                found = LoginShape()
            self._seen[site] = (found, self.clock() + (self.ttl if found.observed else self.miss_ttl))
            return found

    async def _look(self, site: str) -> LoginShape:
        from app.browser import cdp
        from app.browser.provider import browser_session
        from app.sites.loader import hints_for
        first_url = hints_for(site).auth_check_url
        async with browser_session(self.provider, PROBE_USER, headless=True) as session, cdp.default_context(session.cdp_url) as ctx:
            page = await ctx.new_page()
            for old in list(ctx.pages):                   # the shared profile restores earlier tabs: close them, they only slow this down
                if old is not page:
                    try:
                        await old.close()
                    except Exception:
                        pass
            try:
                for url in dict.fromkeys([u for u in (first_url, f"https://{site}") if u]):    # a login-only URL can error for a visitor: try the home page
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                        break
                    except Exception:
                        continue
                else:
                    return LoginShape()
                try:
                    await page.wait_for_load_state("networkidle", timeout=4000)
                except Exception:
                    pass
                first = await self._until_shown(page, site, seconds=4)     # a bot-check page may reload itself once before the site appears
                if first.password:
                    return first
                try:                                      # the real form usually sits behind the page's own "Log in": press it once
                    await page.locator("a,button,[role=button]").filter(has_text=_LOGIN_BUTTON).filter(visible=True).first.click(timeout=5000, no_wait_after=True)   # a click that starts a page load must not wait for it
                    second = await self._until_shown(page, site)
                    if second.observed:
                        return second
                except Exception:
                    pass
                # no sign-in form was reached: a lone email box is as likely a newsletter as a login, so only a phone or username box counts
                return first if first.identifier in ("phone", "username") else LoginShape()
            finally:
                await page.close()

    async def _until_shown(self, page, site: str, seconds: float = 8) -> LoginShape:
        """After pressing "Log in" the form may take a moment (a page load, a modal animating in). Read until fields show or time is up.
        Reading mid-navigation can fail; that just means "not yet"."""
        end = time.monotonic() + seconds
        while True:
            try:
                shape = await self._read(page, site)
            except Exception:
                shape = LoginShape()
            if shape.observed or time.monotonic() >= end:
                return shape
            await page.wait_for_timeout(500)

    @staticmethod
    async def _read(page, site: str) -> LoginShape:
        if registrable_domain(page.url) != registrable_domain(site):      # bounced somewhere else: that is not this site's sign-in
            return LoginShape()
        data = await page.evaluate(_SCAN)
        return classify(data["inputs"], data["buttons"])
