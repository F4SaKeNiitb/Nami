"""Real Chromium: profile persistence/isolation and per-site snapshot, restore and revoke."""

import pytest

from app.browser.cdp import default_context
from app.browser.local_provider import LocalPlaywrightProvider
from app.browser.provider import browser_session
from app.browser.snapshots import SiteSnapshot, clear_site, restore_site, snapshot_site

pytestmark = pytest.mark.browser


@pytest.fixture
async def provider(tmp_path):
    p = LocalPlaywrightProvider(tmp_path / "profiles")
    try:
        s = await p.open("probe")
        await p.close(s)
    except Exception as e:                       # no Chromium installed
        pytest.skip(f"chromium unavailable: {e}")
    yield p
    await p.shutdown()


async def seed(provider, session, *, cookies=(), local=(), persistent=True):
    async with default_context(session.cdp_url) as ctx:
        for name, value, url in cookies:
            c = {"name": name, "value": value, "url": url}
            if persistent:
                c["expires"] = 4102444800        # year 2100; without it Chromium drops the cookie on close
            await ctx.add_cookies([c])
        for origin, key, value in local:
            page = await ctx.new_page()
            await page.route("**/*", lambda r: r.fulfill(body="<html></html>", content_type="text/html"))
            await page.goto(origin + "/")
            await page.evaluate("([k, v]) => localStorage.setItem(k, v)", [key, value])
            await page.close()


async def cookie_names(provider, session):
    async with default_context(session.cdp_url) as ctx:
        return {c["name"] for c in await ctx.cookies()}


# the two origins are different sites for cookies: 127.0.0.1 vs localhost
A, B = "http://127.0.0.1:9", "http://localhost:9"


async def test_profile_persists_across_sessions_for_the_same_user(provider):
    async with browser_session(provider, "alice") as s:
        await seed(provider, s, cookies=[("sid", "abc", A)])
    async with browser_session(provider, "alice") as s:
        assert "sid" in await cookie_names(provider, s)


async def test_profiles_are_isolated_between_users(provider):
    async with browser_session(provider, "alice") as s:
        await seed(provider, s, cookies=[("sid", "alice-session", A)])
    async with browser_session(provider, "bob") as s:
        assert "sid" not in await cookie_names(provider, s)


async def test_session_cookies_do_not_survive_a_browser_restart(provider):
    """Why snapshots exist: a login held in a session cookie is lost with the profile alone."""
    async with browser_session(provider, "alice") as s:
        await seed(provider, s, cookies=[("sid", "abc", A)], persistent=False)
        snap = await snapshot_site(s.cdp_url, A)
    async with browser_session(provider, "alice") as s:
        assert "sid" not in await cookie_names(provider, s)
        await restore_site(s.cdp_url, snap)
        assert "sid" in await cookie_names(provider, s)


def test_profile_path_cannot_escape_the_profiles_directory(tmp_path):
    p = LocalPlaywrightProvider(tmp_path / "profiles")
    path = p.profile_path("../../etc/passwd")
    assert path.parent == tmp_path / "profiles" and ".." not in str(path.relative_to(tmp_path))


async def test_second_open_is_refused_while_browser_is_running(provider):
    from app.browser.provider import ProfileBusy
    async with browser_session(provider, "alice"):
        with pytest.raises(ProfileBusy):
            await provider.open("alice")
    async with browser_session(provider, "alice"):       # and works again afterwards
        pass


async def test_snapshot_contains_only_the_requested_site(provider):
    async with browser_session(provider, "alice") as s:
        await seed(provider, s, cookies=[("a", "1", A), ("b", "2", B)],
                   local=[(A, "token", "tok-A"), (B, "token", "tok-B")])
        snap = await snapshot_site(s.cdp_url, A, origins=[A])
    assert snap.site == "127.0.0.1"
    assert [c["name"] for c in snap.cookies] == ["a"]
    assert snap.local_storage == {A: {"token": "tok-A"}}


async def test_snapshot_restores_into_a_fresh_profile(provider):
    async with browser_session(provider, "alice") as s:
        await seed(provider, s, cookies=[("sid", "S3CR3T", A)], local=[(A, "jwt", "j.w.t")])
        snap = await snapshot_site(s.cdp_url, A, origins=[A])
    data = SiteSnapshot.from_dict(snap.to_dict())                    # survives JSON-shaped round trip
    async with browser_session(provider, "carol") as s:              # brand new profile
        assert "sid" not in await cookie_names(provider, s)
        await restore_site(s.cdp_url, data)
        async with default_context(s.cdp_url) as ctx:
            assert {c["name"]: c["value"] for c in await ctx.cookies()}["sid"] == "S3CR3T"
        again = await snapshot_site(s.cdp_url, A, origins=[A])
        assert again.local_storage == {A: {"jwt": "j.w.t"}}


async def test_tampered_snapshot_cannot_plant_cookies_for_other_sites(provider):
    evil = SiteSnapshot("127.0.0.1", cookies=[
        {"name": "ok", "value": "1", "domain": "127.0.0.1", "path": "/"},
        {"name": "planted", "value": "x", "domain": "localhost", "path": "/"},
        {"name": "planted2", "value": "x", "domain": ".example.com", "path": "/"}],
        local_storage={B: {"planted": "x"}})
    async with browser_session(provider, "alice") as s:
        await restore_site(s.cdp_url, evil)
        assert await cookie_names(provider, s) == {"ok"}
        assert (await snapshot_site(s.cdp_url, B, origins=[B])).local_storage == {}


async def test_clear_site_removes_that_site_only(provider):
    async with browser_session(provider, "alice") as s:
        await seed(provider, s, cookies=[("a", "1", A), ("b", "2", B)],
                   local=[(A, "t", "x"), (B, "t", "y")])
        await clear_site(s.cdp_url, A, origins=[A])
        assert await cookie_names(provider, s) == {"b"}
        assert (await snapshot_site(s.cdp_url, A, origins=[A])).local_storage == {}
        assert (await snapshot_site(s.cdp_url, B, origins=[B])).local_storage == {B: {"t": "y"}}


async def test_clear_site_is_persisted_after_close(provider):
    async with browser_session(provider, "alice") as s:
        await seed(provider, s, cookies=[("a", "1", A)])
        await clear_site(s.cdp_url, A, origins=[A])
    async with browser_session(provider, "alice") as s:
        assert await cookie_names(provider, s) == set()


async def test_snapshot_repr_hides_values():
    snap = SiteSnapshot("x.com", [{"name": "sid", "value": "TOPSECRET"}], {"https://x.com": {"k": "TOPSECRET"}})
    assert "TOPSECRET" not in repr(snap)


async def test_invalid_site_is_rejected(provider):
    async with browser_session(provider, "alice") as s:
        for bad in ("", "not a host!!"):
            with pytest.raises(ValueError):
                await snapshot_site(s.cdp_url, bad)
            with pytest.raises(ValueError):
                await clear_site(s.cdp_url, bad)


# ── the browser a person signs in to must not look automated (Google refuses automated browsers) ──

import subprocess  # noqa: E402

from app.browser.local_provider import CHROME_FLAGS, find_chrome  # noqa: E402

needs_chrome = pytest.mark.skipif(find_chrome() is None, reason="real Google Chrome not installed")


async def _webdriver(session) -> bool:
    async with default_context(session.cdp_url) as ctx:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        return await page.evaluate("navigator.webdriver")


@needs_chrome
@pytest.mark.parametrize("headless", [True, False])
async def test_real_chrome_is_started_without_automation_flags(tmp_path, headless):
    p = LocalPlaywrightProvider(tmp_path / "profiles", browser="chrome")
    s = await p.open("u", headless=headless)
    try:
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(p._procs[s.session_id].pid)], capture_output=True, text=True).stdout
        assert "--enable-automation" not in cmd and "--remote-debugging-pipe" not in cmd
        assert "--user-data-dir=" in cmd and "--remote-debugging-port=" in cmd
        assert ("--headless=new" in cmd) is headless
        assert await _webdriver(s) is False
    finally:
        await p.close(s)
        await p.shutdown()


async def test_the_playwright_fallback_also_hides_the_automation_marker(tmp_path):
    p = LocalPlaywrightProvider(tmp_path / "profiles", browser="playwright")
    try:
        s = await p.open("u", headless=True)
    except Exception as e:
        pytest.skip(f"chromium unavailable: {e}")
    try:
        assert p.chrome is None and await _webdriver(s) is False
    finally:
        await p.close(s)
        await p.shutdown()


@needs_chrome
async def test_closing_chrome_ends_the_process_and_frees_the_profile(tmp_path):
    p = LocalPlaywrightProvider(tmp_path / "profiles", browser="chrome")
    s = await p.open("u", headless=True)
    proc = p._procs[s.session_id]
    await p.close(s)
    assert proc.returncode is not None and not p.is_open("u") and not p._procs and not p._ports
    s2 = await p.open("u", headless=True)                       # the same profile can be opened again straight away
    await p.close(s2)
    await p.shutdown()


@needs_chrome
async def test_a_cookie_written_just_before_close_survives_in_real_chrome(tmp_path):
    """The reason Browser.close is used: a bare SIGTERM lost cookies written seconds earlier."""
    p = LocalPlaywrightProvider(tmp_path / "profiles", browser="chrome")
    for _ in range(3):
        async with browser_session(p, "u") as s:
            await seed(p, s, cookies=[("sid", "abc", A)])
        async with browser_session(p, "u") as s:
            assert "sid" in await cookie_names(p, s)
    await p.shutdown()


@needs_chrome
async def test_a_second_open_is_still_refused_with_real_chrome(tmp_path):
    from app.browser.provider import ProfileBusy
    p = LocalPlaywrightProvider(tmp_path / "profiles", browser="chrome")
    s = await p.open("u", headless=True)
    try:
        with pytest.raises(ProfileBusy):
            await p.open("u", headless=True)
    finally:
        await p.close(s)
        await p.shutdown()


def test_choosing_chrome_when_it_is_missing_fails_loudly_and_unknown_modes_are_rejected(tmp_path):
    with pytest.raises(RuntimeError, match="Chrome was not found"):
        LocalPlaywrightProvider(tmp_path, browser="chrome", chrome_path="/nonexistent/chrome")
    with pytest.raises(ValueError):
        LocalPlaywrightProvider(tmp_path, browser="firefox")
    assert LocalPlaywrightProvider(tmp_path, browser="playwright").chrome is None
    assert find_chrome("/nonexistent/chrome") is None


def test_chrome_flags_never_include_an_automation_switch():
    assert not any("automation" in f or "pipe" in f for f in CHROME_FLAGS)


# ── a crashed server must not leave a Chrome that blocks the profile ──

import os as _os  # noqa: E402
import signal as _signal  # noqa: E402


def _alive(pid: int) -> bool:
    try:
        _os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return bool(subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout.strip())


@needs_chrome
async def test_a_chrome_left_behind_by_a_crashed_server_is_cleaned_up_on_the_next_open(tmp_path):
    first = LocalPlaywrightProvider(tmp_path / "profiles", browser="chrome")
    s1 = await first.open("u", headless=True)
    await seed(first, s1, cookies=[("sid", "abc", A)])
    old_pid = first._procs[s1.session_id].pid
    # the server "crashes": the provider object is gone, nothing was closed, Chrome keeps running
    second = LocalPlaywrightProvider(tmp_path / "profiles", browser="chrome")
    assert _alive(old_pid)
    s2 = await second.open("u", headless=True)
    try:
        assert not _alive(old_pid)                                   # the leftover was stopped, not handed off to
        assert second._procs[s2.session_id].pid != old_pid and await _webdriver(s2) is False
    finally:
        await second.close(s2)
        await second.shutdown()
        for pid in (old_pid,):
            if _alive(pid):
                _os.kill(pid, _signal.SIGKILL)


@needs_chrome
async def test_a_stale_record_with_a_recycled_pid_never_kills_an_unrelated_process(tmp_path):
    p = LocalPlaywrightProvider(tmp_path / "profiles", browser="chrome")
    victim = subprocess.Popen(["sleep", "60"])                       # an unrelated process that happens to own the recorded pid
    try:
        pidfile = p._pidfile(p.profile_path("u"))
        pidfile.parent.mkdir(parents=True, exist_ok=True)
        pidfile.write_text('{"pid": %d, "port": 9}' % victim.pid)
        s = await p.open("u", headless=True)
        assert victim.poll() is None                                 # still running: its command line is not our Chrome profile
        await p.close(s)
    finally:
        victim.kill()
        await p.shutdown()


@needs_chrome
async def test_the_record_is_removed_on_a_clean_close_and_garbage_records_are_ignored(tmp_path):
    p = LocalPlaywrightProvider(tmp_path / "profiles", browser="chrome")
    pidfile = p._pidfile(p.profile_path("u"))
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text("not json")
    s = await p.open("u", headless=True)
    assert pidfile.exists()
    await p.close(s)
    assert not pidfile.exists()
    await p.shutdown()


# ── server shutdown must close the browsers before the shared CDP driver goes away ──

async def test_runtime_close_stops_the_browser_provider_before_the_cdp_driver(monkeypatch):
    from types import SimpleNamespace

    from app.browser import cdp
    from app.core.runtime import Runtime
    order = []

    class Provider:
        async def shutdown(self):
            order.append("provider")

    class Stack:
        async def aclose(self):
            order.append("stack")

    class Services:
        async def close(self):
            order.append("services")

    async def cdp_shutdown():
        order.append("cdp")
    monkeypatch.setattr(cdp, "shutdown", cdp_shutdown)
    rt = object.__new__(Runtime)
    rt.__dict__.update(orchestrator=SimpleNamespace(harness=None), provider=Provider(), _stack=Stack(), services=Services())
    monkeypatch.setattr("app.core.runtime.tel", SimpleNamespace(shutdown=lambda: order.append("tel")))
    await rt.close()
    assert order.index("provider") < order.index("cdp")
    assert order[-2:] == ["stack", "services"]


# ── form history must not be kept in the profile (found live: typed usernames, cards and codes sat in `Web Data`) ──

def test_harden_profile_turns_off_autofill_and_keeps_other_preferences(tmp_path):
    import json

    from app.browser.local_provider import harden_profile
    (tmp_path / "Default").mkdir()
    (tmp_path / "Default" / "Preferences").write_text(json.dumps({"homepage": "x", "autofill": {"other": 1}}))
    harden_profile(tmp_path)
    prefs = json.loads((tmp_path / "Default" / "Preferences").read_text())
    assert prefs["autofill"] == {"other": 1, "profile_enabled": False, "credit_card_enabled": False}
    assert prefs["credentials_enable_service"] is False and prefs["profile"]["password_manager_enabled"] is False
    assert prefs["homepage"] == "x"


def test_harden_profile_deletes_form_history_already_written(tmp_path):
    from app.browser.local_provider import harden_profile
    d = tmp_path / "Default"
    d.mkdir()
    (d / "Web Data").write_bytes(b"4111111111111111")
    (d / "Web Data-journal").write_bytes(b"x")
    (d / "Cookies").write_bytes(b"keep")
    harden_profile(tmp_path)
    assert not (d / "Web Data").exists() and not (d / "Web Data-journal").exists()
    assert (d / "Cookies").read_bytes() == b"keep"                          # sessions are untouched


def test_harden_profile_survives_a_corrupt_preferences_file(tmp_path):
    import json

    from app.browser.local_provider import harden_profile
    (tmp_path / "Default").mkdir()
    (tmp_path / "Default" / "Preferences").write_text("{not json")
    harden_profile(tmp_path)
    assert json.loads((tmp_path / "Default" / "Preferences").read_text())["autofill"]["profile_enabled"] is False


async def test_a_typed_value_is_not_left_in_the_profile_after_the_browser_closes(provider, tmp_path):
    """Real Chrome: type into a form, submit, close; the typed text must not be in any profile file."""
    import http.server
    import secrets
    import threading

    from app.browser.cdp import connect
    marker = "marker" + secrets.token_hex(6)

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): ...

        def do_GET(self):
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
            self.wfile.write(b'<form method="post" action="/"><input name="card" id="c"><button id="b">go</button></form>')

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(302); self.send_header("Location", "/"); self.end_headers()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        s = await provider.open("typed")
        async with connect(s.cdp_url) as b:
            page = b.contexts[0].pages[0] if b.contexts[0].pages else await b.contexts[0].new_page()
            await page.goto(f"http://127.0.0.1:{srv.server_address[1]}/")
            await page.fill("#c", marker)
            await page.click("#b")
            await page.wait_for_load_state("load")
        await provider.close(s)
        found = [str(p) for p in provider.profile_path("typed").rglob("*") if p.is_file() and marker.encode() in p.read_bytes()]
        assert found == []
    finally:
        srv.shutdown()


async def test_revoking_a_site_clears_its_cookies_from_the_saved_profile_with_no_browser_open(provider):
    """The live bug: 'Revoke' left the site signed in because the cookies stayed in the profile on disk."""
    import contextlib
    from types import SimpleNamespace
    from app.browser.session_manager import SessionManager

    async def nothing(*a, **k):
        return None

    @contextlib.asynccontextmanager
    async def no_db():                              # there is no stored snapshot here: only the profile clearing is under test
        yield SimpleNamespace(scalar=nothing, delete=nothing)

    async with browser_session(provider, "carol") as s:
        await seed(provider, s, cookies=[("keep", "1", "http://localhost:9"), ("sid", "2", "http://127.0.0.1:9")])
    assert not provider.is_open("carol")
    assert await SessionManager(SimpleNamespace(session=no_db), None, provider=provider).revoke("carol", "127.0.0.1") is True
    async with browser_session(provider, "carol") as s:
        names = await cookie_names(provider, s)
    assert names == {"keep"}                        # the revoked site's cookie is gone, another site's is untouched
