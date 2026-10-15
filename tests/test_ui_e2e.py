"""Drives the real app in Chromium: sign-in, secret redaction in chat, the secure form, Connections, history.

Runs from a scratch copy of the project so it never touches data/ (databases, dev key). Needs Chromium.
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from playwright.async_api import async_playwright

pytestmark = pytest.mark.browser
ROOT = Path(__file__).resolve().parent.parent
API_KEY = "sk-or-v1-abcdef0123456789abcdef0123456789"
PW = "Tr0ub4dor&3-unique-marker"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    work = tmp_path_factory.mktemp("app")
    for name in ("app", "migrations", "public", ".chainlit", "alembic.ini", "chainlit.md", "pyproject.toml"):
        (work / name).symlink_to(ROOT / name)
    port = free_port()
    env = {**os.environ, "NAMI_DEV_AUTH": "1", "NAMI_DEV_PASSWORD": "dev", "NAMI_ANTHROPIC_API_KEY": "",
           "NAMI_OPENAI_API_KEY": "", "NAMI_OPENROUTER_API_KEY": "", "NAMI_LLM_PROVIDER": "anthropic",
           "CHAINLIT_AUTH_SECRET": "test-secret-" + "x" * 40, "NAMI_HEADLESS": "true", "PYTHONPATH": str(work),
           "NAMI_DATABASE_URL": f"sqlite+aiosqlite:///{work}/data/nami.db", "NAMI_REDIS_URL": "",
           "OAUTH_GOOGLE_CLIENT_ID": "", "OAUTH_GOOGLE_CLIENT_SECRET": ""}
    log = open(work / "server.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)], cwd=work, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    for _ in range(120):
        try:
            if httpx.get(url + "/healthz", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.5)
    else:
        proc.kill()
        pytest.fail("server did not start:\n" + (work / "server.log").read_text()[-3000:])
    yield url, work
    proc.terminate()
    proc.wait(timeout=20)


@pytest.fixture
async def page(server):
    url, _ = server
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context()
        pg = await ctx.new_page()
        await pg.goto(url + "/chainlit/login")
        await pg.fill("input[name=email]", "dev@nami.local")
        await pg.fill("input[name=password]", "dev")
        await pg.click("button[type=submit]")
        await pg.wait_for_selector("#chat-input", timeout=30000)
        await pg.wait_for_selector("text=background browser", timeout=30000)      # welcome message: the socket session is live
        yield pg
        await browser.close()


async def test_wrong_password_is_rejected(server):
    url, _ = server
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page()
        await pg.goto(url + "/chainlit/login")
        await pg.fill("input[name=email]", "dev@nami.local")
        await pg.fill("input[name=password]", "nope")
        await pg.click("button[type=submit]")
        await pg.wait_for_timeout(1500)
        assert "/login" in pg.url and await pg.locator("#chat-input").count() == 0
        await b.close()


async def test_vault_api_needs_the_session_cookie(server):
    url, _ = server
    assert httpx.get(url + "/chainlit/vault/credentials").status_code == 401
    assert httpx.get(url + "/admin/metrics").status_code in (401, 404)   # anonymous: Chainlit answers 401 before our 404


async def test_pasted_secret_is_redacted_and_never_answered(page):
    await page.fill("#chat-input", f"here is my key {API_KEY}")
    await page.keyboard.press("Enter")
    await page.wait_for_selector("text=looks like a secret", timeout=20000)
    body = await page.inner_text("body")
    assert "[REDACTED" in body
    await page.reload()                                  # what a returning user (or the sidebar) sees comes from storage
    await page.wait_for_selector("#chat-input", timeout=30000)
    assert API_KEY not in await page.inner_text("body")


def test_secret_is_not_in_any_database_file_or_log(server):
    _, work = server
    import glob
    blob = b"".join(open(f, "rb").read() for f in glob.glob(str(work / "data" / "*")) if os.path.isfile(f))
    assert API_KEY.encode() not in blob and PW.encode() not in blob
    assert API_KEY not in (work / "server.log").read_text()


async def test_task_without_model_key_says_so_plainly(page):
    await page.fill("#chat-input", "Check my OpenRouter credits")
    await page.keyboard.press("Enter")
    await page.wait_for_selector("text=No model key", timeout=30000)


async def test_secure_form_saves_via_vault_and_secret_never_appears_in_the_page(page, server):
    # same-origin fetch with the session cookie, exactly what SecureInput does
    res = await page.evaluate("""async (pw) => {
        const r = await fetch('/chainlit/vault/credentials', {method:'POST', credentials:'include',
          headers:{'Content-Type':'application/json','X-Requested-With':'nami'},
          body: JSON.stringify({kind:'password', label:'Acme', origins:['acme.com'], secret:{username:'me', password:pw}})});
        return [r.status, await r.text()];
    }""", PW)
    assert res[0] == 200 and PW not in res[1]
    listing = await page.evaluate("async () => (await fetch('/chainlit/vault/credentials', {credentials:'include'})).text()")
    assert "Acme" in listing and PW not in listing
    blocked = await page.evaluate("""async () => (await fetch('/chainlit/vault/credentials', {method:'POST', credentials:'include',
          headers:{'Content-Type':'application/json'}, body:'{}'})).status""")
    assert blocked == 403                                    # CSRF header missing
    log = (server[1] / "server.log").read_text()
    assert PW not in log and API_KEY not in log


async def test_no_errors_in_server_log(server):
    log = (server[1] / "server.log").read_text()
    assert "Traceback" not in log, log[-3000:]


async def test_connections_sidebar_renders_vault_sessions_and_learned_workflows(page):
    await page.wait_for_selector("text=Saved logins", timeout=30000)
    body = await page.inner_text("body")
    assert "Learned workflows" in body and "Signed-in sites" in body
    assert "Nothing learned yet" in body                      # empty state comes from GET /chainlit/skills (200, not an error)
    assert "Error 4" not in body and "Error 5" not in body


async def test_skills_api_needs_login_and_csrf(server, page):
    url, _ = server
    assert httpx.get(url + "/chainlit/skills").status_code == 401
    status = await page.evaluate("""async () => (await fetch('/chainlit/skills/abc', {method:'DELETE', credentials:'include'})).status""")
    assert status == 403
    status = await page.evaluate("""async () => (await fetch('/chainlit/skills/abc', {method:'DELETE', credentials:'include',
        headers:{'X-Requested-With':'nami'}})).status""")
    assert status == 404
