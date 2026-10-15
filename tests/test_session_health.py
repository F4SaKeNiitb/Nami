"""The sign-in health check on a real browser: a signed-out landing page must not pass as 'signed in'."""

import http.server
import threading

import pytest

from app.browser import cdp
from app.browser.local_provider import LocalPlaywrightProvider
from app.browser.provider import browser_session
from app.browser.session_manager import SessionManager
from app.sites.loader import SiteHints

pytestmark = pytest.mark.browser


class Site(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/account/about"):
            self._page("<h1>Sign in to your account</h1>")
        elif self.path.startswith("/bounce"):
            self.send_response(302); self.send_header("Location", "/account/about/"); self.end_headers()
        elif self.path.startswith("/login"):
            self._page("<input type=password>")
        elif self.path.startswith("/js-bounce"):
            self._page("<script>location.replace('/account/about/')</script>")
        else:
            self._page("<h1>Welcome back</h1>")

    def _page(self, body):
        data = f"<html><body>{body}</body></html>".encode()
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(data)

    def log_message(self, *a): ...


@pytest.fixture
async def world(tmp_path):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    p = LocalPlaywrightProvider(tmp_path / "profiles")
    try:
        probe = await p.open("probe")
        await p.close(probe)
    except Exception as e:
        pytest.skip(f"browser unavailable: {e}")
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield p, SessionManager(None, None), base
    srv.shutdown()
    await p.shutdown()
    await cdp.shutdown()


def hints(base, path, **kw):
    return SiteHints(site="127.0.0.1", auth_check_url=base + path, login_patterns=("/login", "/account/about"), **kw)


async def check(world, path, **kw):
    p, sm, base = world
    async with browser_session(p, "u") as s:
        return await sm.is_logged_in(s.cdp_url, hints(base, path, **kw))


async def test_a_page_that_stays_put_counts_as_signed_in(world):
    assert await check(world, "/dashboard") is True


async def test_a_redirect_to_a_signed_out_landing_page_is_not_signed_in(world):
    """The real bug: google.com/account/about/ was not in the list, so an expired session passed the health check."""
    assert await check(world, "/bounce") is False


async def test_a_client_side_redirect_to_the_landing_page_is_caught_too(world):
    assert await check(world, "/js-bounce") is False


async def test_a_password_box_means_signed_out(world):
    assert await check(world, "/login") is False


async def test_the_expected_page_must_be_where_the_check_ends(world):
    p, sm, base = world
    assert await check(world, "/dashboard", expect_url="/dashboard") is True
    assert await check(world, "/dashboard", expect_url="myaccount.example") is False      # quiet 200 on the wrong page is not enough


async def test_a_site_without_an_auth_check_url_is_assumed_fine(world):
    p, sm, base = world
    async with browser_session(p, "u") as s:
        assert await sm.is_logged_in(s.cdp_url, SiteHints(site="x")) is True
