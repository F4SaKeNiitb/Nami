"""Origin binding: lookalike and attacker domains must never match."""

import pytest

from app.vault.origins import normalize_allowlist, origin_allowed, registrable_domain

ALLOW = ["swiggy.com"]


@pytest.mark.parametrize("url", [
    "https://swiggy.com", "https://www.swiggy.com/orders", "https://a.b.swiggy.com/x?y=1",
    "https://SWIGGY.com", "swiggy.com",
])
def test_allowed(url):
    assert origin_allowed(url, ALLOW)


@pytest.mark.parametrize("url", [
    "https://swiggy.com.evil.com",          # suffix trick
    "https://evil.com/swiggy.com",          # path trick
    "https://evil.com?u=swiggy.com",        # query trick
    "https://swiggy.com@evil.com",          # userinfo trick
    "https://notswiggy.com", "https://swiggy.co", "https://swlggy.com", "https://swiggy-com.evil.net",
    "http://swiggy.com",                    # downgrade
    "ftp://swiggy.com", "javascript:alert(1)", "", "   ", "https://", "about:blank",
    "https://evil.co.uk",
])
def test_refused(url):
    assert not origin_allowed(url, ALLOW)


def test_multi_part_public_suffix():
    assert registrable_domain("https://shop.example.co.uk") == "example.co.uk"
    assert not origin_allowed("https://example.co.uk.evil.co.uk", ["example.co.uk"])


def test_localhost_only_when_listed_and_http_only_for_it():
    assert origin_allowed("http://localhost:8000/login", ["localhost"])
    assert not origin_allowed("http://localhost:8000", ["swiggy.com"])
    assert not origin_allowed("http://swiggy.com", ["localhost"])


def test_empty_allowlist_allows_nothing():
    assert not origin_allowed("https://swiggy.com", [])


def test_normalize_dedupes_and_drops_junk():
    assert normalize_allowlist(["https://www.swiggy.com/x", "swiggy.com", "not a host!!", ""]) == ["swiggy.com"]
