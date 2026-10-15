from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import re

import yaml

_PATH = Path(__file__).with_name("registry.yaml")


@dataclass(frozen=True)
class SiteHints:
    site: str
    auth_check_url: str | None = None
    google_signin: bool = False
    human_step: str | None = None              # sms|email|captcha|passkey|google: a step the user may still have to do
    notes: list[str] = field(default_factory=list)
    login_patterns: tuple[str, ...] = ()
    expect_url: str | None = None              # a signed-in auth check must END on a URL containing this
    api: dict | None = None                    # {base, auth, endpoints: {name: {path, summary}}}: read-only API alternative to the browser


@lru_cache
def _load() -> dict:
    return yaml.safe_load(_PATH.read_text())


def canonical_site(site: str) -> str:
    """The domain a login actually lives on (gmail.com -> google.com). Unknown sites are returned unchanged."""
    return _load().get("aliases", {}).get(site.lower().strip(), site)


_AMBIGUOUS = {"google.com"}            # "google" is a verb and a prefix of Gmail, Drive and Docs: those are the model's judgement, not a word match


def sites_named(text: str) -> list[str]:
    """Registry services the user's own words name outright ("openrouter", "swiggy"), in the order they appear. Plain code over the user's text only,
    so neither a model's miss nor a web page can change it. Found live: "generate a poem using my openrouter API key" came back with no site at all
    and the task stopped with "I need to know which website to use"."""
    found: list[tuple[int, str]] = []
    for site in _load()["sites"]:
        if site in _AMBIGUOUS:
            continue
        brand = site.split(".")[0]
        m = re.search(rf"(?<![a-z0-9]){re.escape(brand)}(?![a-z0-9])", text or "", re.I)
        if m:
            found.append((m.start(), site))
    return [site for _, site in sorted(found)]


def start_urls(sites: list[str], allowed_domains: list[str], limit: int = 3) -> list[str]:
    """Where to start, from the sites the user named: the HOST they meant ("news.ycombinator.com"), not the registrable domain the access plan
    keeps ("ycombinator.com"), which would send a reader to the wrong front page (found live: "top stories on hacker news" opened ycombinator.com).
    Only hosts inside the plan's allowed domains are used, so this can never widen what a task may visit."""
    from urllib.parse import urlsplit
    from app.vault.origins import registrable_domain
    ok, out = set(allowed_domains), []
    for site in sites:
        host = (urlsplit(site if "://" in site else f"//{site}").hostname or "").lower()
        if host and registrable_domain(host) in ok and f"https://{host}/" not in out:
            out.append(f"https://{host}/")
    return out[:limit]


def generation_sites() -> list[str]:
    """Sites whose registry entry declares generate endpoints (text/image/video/speech through an API key)."""
    return [site for site, d in _load()["sites"].items()
            if any(e.get("kind") == "generate" for e in ((d.get("api") or {}).get("endpoints") or {}).values())]


def hints_for(site: str) -> SiteHints:
    data = _load()
    site = canonical_site(site)
    d = data["sites"].get(site, {})
    return SiteHints(site=site, auth_check_url=d.get("auth_check_url"),
                     google_signin=d.get("google_signin", False), human_step=d.get("human_step"), notes=list(d.get("notes", [])),
                     login_patterns=tuple(data["default"]["login_patterns"]) + tuple(d.get("signed_out_patterns", [])),
                     expect_url=d.get("expect_url"), api=d.get("api"))


def looks_logged_out(url: str, hints: SiteHints) -> bool:
    return any(p in url.lower() for p in hints.login_patterns)
