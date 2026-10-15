"""Local browser with a persistent profile per user. Same contract as Kernel Profiles.

Why not just Playwright's Chromium: it is started with --enable-automation and sets navigator.webdriver, and Google refuses to
sign in to such a browser ("This browser or app may not be secure"). When real Chrome is installed we start it ourselves as
an ordinary process (own profile folder, a debugging port, no automation flags) and attach over CDP only afterwards, so the
window a person types their password into looks like any other Chrome. Without Chrome we fall back to Playwright's Chromium
with the automation switches turned off, which is better but less reliable against Google.
"""

import asyncio
import hashlib
import json
import logging
import os
import signal
import shutil
import socket
import subprocess
import sys
import uuid
from pathlib import Path

from playwright.async_api import BrowserContext, Playwright, async_playwright

from app.browser.provider import BrowserProvider, BrowserSession

log = logging.getLogger("nami.local_browser")

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)
CHROME_FLAGS = ("--no-first-run", "--no-default-browser-check", "--disable-search-engine-choice-screen",
                "--password-store=basic", "--use-mock-keychain")        # no OS keychain prompt for a throwaway profile


# Chrome keeps what is typed into text fields (a username, a card number, a one-time code) in plain text in `Web Data` inside the
# profile folder, outside the vault. Measured live: a username typed into a sign-in form was found there after the browser closed.
# Switching autofill off in the profile's preferences stops it (measured: nothing left in the profile).
_PRIVATE_PREFS = {"autofill": {"profile_enabled": False, "credit_card_enabled": False}, "credentials_enable_service": False,
                  "profile": {"password_manager_enabled": False}}


def harden_profile(path: Path) -> None:
    """Turn off form-history autofill and the password manager for this profile, and drop any form history already written.
    Call before the browser starts (Chrome rewrites these files while it runs)."""
    default = path / "Default"
    default.mkdir(parents=True, exist_ok=True)
    prefs_file = default / "Preferences"
    try:
        prefs = json.loads(prefs_file.read_text()) if prefs_file.exists() else {}
        if not isinstance(prefs, dict):
            prefs = {}
    except (OSError, ValueError):
        prefs = {}
    for key, value in _PRIVATE_PREFS.items():
        if isinstance(value, dict):
            cur = prefs.get(key)
            prefs[key] = {**(cur if isinstance(cur, dict) else {}), **value}
        else:
            prefs[key] = value
    prefs_file.write_text(json.dumps(prefs))
    for name in ("Web Data", "Web Data-journal"):
        (default / name).unlink(missing_ok=True)


_UA_CACHE: dict[str, str] = {}


def headless_flags(chrome: str) -> list[str]:
    """Headless Chrome says "HeadlessChrome" in its user agent, which sign-in pages (Google first) refuse. Present the same
    user agent the visible Chrome of this version sends, and a normal laptop-sized window."""
    ua = _UA_CACHE.get(chrome)
    if ua is None:
        try:
            out = subprocess.run([chrome, "--version"], capture_output=True, text=True, timeout=10).stdout
            major = next((w.split(".")[0] for w in out.split() if w[:1].isdigit()), "")
        except Exception:
            major = ""
        if major:
            platform = {"darwin": "Macintosh; Intel Mac OS X 10_15_7", "win32": "Windows NT 10.0; Win64; x64"}.get(
                sys.platform, "X11; Linux x86_64")
            ua = f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
        _UA_CACHE[chrome] = ua = ua or ""
    return ["--headless=new", "--window-size=1280,800", *([f"--user-agent={ua}"] if ua else [])]


def find_chrome(explicit: str | None = None) -> str | None:
    """Real Google Chrome only (not Chromium or Brave): that is the browser Google's sign-in accepts."""
    if explicit:                                   # an explicit path is the only one tried: a typo must not silently use another
        return explicit if Path(explicit).is_file() and os.access(explicit, os.X_OK) else None
    for path in CHROME_CANDIDATES:
        if Path(path).is_file() and os.access(path, os.X_OK):
            return path
    return shutil.which("google-chrome")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_for_port(port: int, proc: asyncio.subprocess.Process, timeout: float = 20.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if proc.returncode is not None:
            raise RuntimeError(f"Chrome exited immediately (code {proc.returncode})")
        try:
            _, w = await asyncio.open_connection("127.0.0.1", port)
            w.close()
            return
        except OSError:
            await asyncio.sleep(0.15)
    raise RuntimeError("Chrome did not open its debugging port in time")


def _command_line(pid: int) -> str:
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


class LocalPlaywrightProvider(BrowserProvider):
    def __init__(self, profiles_dir: str | Path = "data/profiles", *, browser: str = "auto", chrome_path: str | None = None):
        """browser: "auto" (real Chrome if installed, else Playwright Chromium), "chrome" (error if missing), "playwright"."""
        super().__init__()
        self.profiles_dir = Path(profiles_dir)
        self._pw: Playwright | None = None
        self._contexts: dict[str, BrowserContext] = {}
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._ports: dict[str, int] = {}
        self._pidfiles: dict[str, Path] = {}
        self._scratch_dirs: dict[str, Path] = {}
        if browser not in ("auto", "chrome", "playwright"):
            raise ValueError(f"unknown local browser {browser!r}")
        self.chrome = None if browser == "playwright" else find_chrome(chrome_path)
        if browser == "chrome" and not self.chrome:
            raise RuntimeError("NAMI_LOCAL_BROWSER=chrome but Google Chrome was not found; set NAMI_CHROME_PATH")

    def profile_path(self, user_id: str) -> Path:
        # Hash: a user id can never walk out of the profiles directory.
        return self.profiles_dir / hashlib.sha256(user_id.encode()).hexdigest()[:24]

    def _pidfile(self, path: Path) -> Path:
        return path.with_name(path.name + ".chrome.json")

    async def _reap_orphan(self, path: Path) -> None:
        """Chrome is a plain process, so it outlives a crashed server and keeps the profile folder locked (a new Chrome on the
        same folder would hand off to the old one and exit). Stop a leftover Chrome, but only if its command line really
        carries THIS profile folder, so a recycled pid can never get an unrelated process killed."""
        pidfile = self._pidfile(path)
        try:
            info = json.loads(pidfile.read_text())
            pid, port = int(info["pid"]), int(info["port"])
        except (OSError, ValueError, KeyError, TypeError):
            return
        pidfile.unlink(missing_ok=True)
        if os.name == "nt" or f"--user-data-dir={path.resolve()}" not in _command_line(pid):
            return
        log.warning("stopping a leftover Chrome (pid %s) that still holds the profile", pid)
        for sig in (None, signal.SIGTERM, signal.SIGKILL):
            if sig is None:
                try:
                    from app.browser import cdp
                    async with cdp.connect(f"http://127.0.0.1:{port}") as browser:
                        await (await browser.new_browser_cdp_session()).send("Browser.close")
                except Exception:
                    pass
            else:
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    return
            for _ in range(40):
                if not _command_line(pid):
                    return
                await asyncio.sleep(0.25)

    async def _open(self, user_id: str, *, headless: bool, scratch: bool = False) -> BrowserSession:
        port = _free_port()
        path = self.profiles_dir / f"scratch-{uuid.uuid4().hex[:16]}" if scratch else self.profile_path(user_id)      # a scratch browser never touches the saved profile
        path.mkdir(parents=True, exist_ok=True)
        await self._reap_orphan(path)
        harden_profile(path)
        sid = uuid.uuid4().hex
        if scratch:
            self._scratch_dirs[sid] = path
        if self.chrome:
            args = [self.chrome, f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
                    f"--user-data-dir={path.resolve()}", *CHROME_FLAGS, *(headless_flags(self.chrome) if headless else []), "about:blank"]
            proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                                                        start_new_session=sys.platform != "win32")
            try:
                await _wait_for_port(port, proc)
            except BaseException:
                await self._stop(proc)
                raise
            self._procs[sid] = proc
            self._ports[sid] = port
            self._pidfiles[sid] = self._pidfile(path)
            self._pidfiles[sid].write_text(json.dumps({"pid": proc.pid, "port": port}))
            return BrowserSession(sid, user_id, f"http://127.0.0.1:{port}")
        if self._pw is None:
            self._pw = await async_playwright().start()
        ctx = await self._pw.chromium.launch_persistent_context(
            str(path), headless=headless, ignore_default_args=["--enable-automation"],
            args=[f"--remote-debugging-port={port}", "--disable-blink-features=AutomationControlled"])
        self._contexts[sid] = ctx
        return BrowserSession(sid, user_id, f"http://127.0.0.1:{port}")

    @staticmethod
    async def _stop(proc: asyncio.subprocess.Process, port: int | None = None) -> None:
        """Ask Chrome to close itself (DevTools Browser.close flushes cookies and storage to the profile), then fall back
        to SIGTERM and finally SIGKILL. A bare SIGTERM can lose cookies written in the last seconds."""
        if proc.returncode is None and port is not None:
            try:
                from app.browser import cdp
                async with cdp.connect(f"http://127.0.0.1:{port}") as browser:
                    await (await browser.new_browser_cdp_session()).send("Browser.close")
            except Exception:
                pass                                 # already closing, or CDP unreachable: fall through to signals
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except asyncio.TimeoutError:
                pass
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

    async def _close(self, session: BrowserSession) -> None:
        proc = self._procs.pop(session.session_id, None)
        port = self._ports.pop(session.session_id, None)
        if proc is not None:
            await self._stop(proc, port)
        if (pf := self._pidfiles.pop(session.session_id, None)) is not None:
            pf.unlink(missing_ok=True)
        ctx = self._contexts.pop(session.session_id, None)
        if ctx is not None:
            await ctx.close()          # flushes the profile to disk
        if (d := self._scratch_dirs.pop(session.session_id, None)) is not None:
            shutil.rmtree(d, ignore_errors=True)

    async def shutdown(self) -> None:
        for s in list(self._procs):
            await self._stop(self._procs.pop(s), self._ports.pop(s, None))
            if (pf := self._pidfiles.pop(s, None)) is not None:
                pf.unlink(missing_ok=True)

        for s in list(self._contexts):
            await self._contexts.pop(s).close()
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None
