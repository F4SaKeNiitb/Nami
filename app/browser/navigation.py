"""Move around a site like a person: start from its home page and click the links that are there. Do not type addresses.

Found live: asked to sign in to Swiggy, the agent said "navigate directly to the login page by trying common login URL patterns"
and opened guessed paths (/auth/login), landing on error pages, instead of clicking "Log in" on the home page it was already on.

The agent's `navigate` action is wrapped so an address is allowed only if it is
  * a site's home page (what a person types first),
  * the page the browser is already on (a reload),
  * an address written in the step itself or in the site's own registry entry (the user or the planner gave it).
Anything else is refused with a plain instruction to click through instead. This is code, not a request in the prompt, so a model
that ignores the prompt (or a page that tells it to visit somewhere) still cannot open a made-up link. Pure and tested on its own.
"""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.vault.origins import registrable_domain

GUESSED = ("I did not open that address: it was not a link on a page you have seen, so it may not exist. Do not guess URLs or "
           "paths. Go to the site's home page, or click the link, menu item or button on the page that leads there (scroll, or open "
           "the menu, to find it).")

NAVIGATION_RULE = (
    "Move around a website the way a person does. Start from its home page (or the page you are already on) and click the visible "
    "links, menu items and buttons. Never invent or guess a URL or path such as /login, /auth/login or /orders. You may open a "
    "site's home page, or an address that is written in your task. If the link you need is not visible, scroll, open the menu, or "
    "use the site's own search box."
)

_URL = re.compile(r"https?://[^\s)\]\"'<>]+")


@dataclass(frozen=True)
class NavDecision:
    allow: bool
    reason: str = ""


def _key(u) -> tuple[str, str, str]:
    host = (u.hostname or "").lower()
    return (host[4:] if host.startswith("www.") else host, u.path.rstrip("/"), u.query)


def urls_in(*texts: str) -> list[str]:
    """Addresses written in the given texts (the step's goal, the site's registry entry)."""
    return [m.rstrip(".,;:") for t in texts for m in _URL.findall(t or "")]


def navigation_decision(url: str, *, current_url: str, allowed_domains: list[str], mentioned: list[str]) -> NavDecision:
    u = urlsplit((url or "").strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        return NavDecision(False, "That is not a web address. Click a link on the page instead.")
    ok_sites = {registrable_domain(d) for d in allowed_domains}
    if registrable_domain(u.hostname) not in ok_sites:
        return NavDecision(False, "That address is outside the sites this task may use.")
    if u.path in ("", "/") and not u.query:
        return NavDecision(True)                                          # a site's home page: where a person starts
    key = _key(u)
    if current_url and key == _key(urlsplit(current_url)):
        return NavDecision(True)                                          # reload
    if any(key == _key(urlsplit(m)) for m in mentioned):
        return NavDecision(True)                                          # written down by the user or the planner, not made up
    return NavDecision(False, GUESSED)


def install(tools, *, allowed_domains: list[str], mentioned: list[str], on_block) -> bool:
    """Wrap the agent's `navigate` action with the rule above. `on_block(url)` is awaited whenever one is refused.
    Returns False if the action is not there (a browser-use version without it), so the caller can know."""
    from browser_use import ActionResult
    action = tools.registry.registry.actions.get("navigate")
    if action is None:
        return False
    original = action.function

    async def guarded(params, **ctx):
        d = navigation_decision(params.url, current_url=ctx.get("page_url") or "", allowed_domains=allowed_domains, mentioned=mentioned)
        if not d.allow:
            await on_block(params.url)
            return ActionResult(error=d.reason)
        return await original(params=params, **ctx)

    action.function = guarded
    return True
