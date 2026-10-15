"""Origin binding: a credential may only be used on the registrable domains the user approved."""

from urllib.parse import urlsplit

import tldextract

# Offline: use the bundled public-suffix snapshot, never fetch it at runtime.
_extract = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)
_LOCAL_HOSTS = {"localhost", "127.0.0.1"}


def registrable_domain(url_or_host: str) -> str | None:
    """'https://www.swiggy.com/x' -> 'swiggy.com'. 'swiggy.com.evil.co.uk' -> 'evil.co.uk'."""
    raw = url_or_host.strip().lower()
    host = urlsplit(raw if "://" in raw else f"//{raw}").hostname or ""
    if host in _LOCAL_HOSTS:
        return host
    return _extract(host).top_domain_under_public_suffix or None


def normalize_allowlist(origins: list[str]) -> list[str]:
    out = sorted({d for o in origins if (d := registrable_domain(o))})
    return out


def origin_allowed(origin_url: str, allowlist: list[str]) -> bool:
    """True when the page's registrable domain is on the list. Plain http only for localhost."""
    raw = origin_url.strip().lower()
    parts = urlsplit(raw if "://" in raw else f"//{raw}")
    domain = registrable_domain(raw)
    if not domain or domain not in allowlist:
        return False
    if parts.scheme and parts.scheme not in ("https", "http"):
        return False
    return parts.scheme != "http" or domain in _LOCAL_HOSTS
