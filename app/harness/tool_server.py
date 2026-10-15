"""The only door pi has to the web: a loopback server whose every call is checked by Nami's own code.

Pi runs as a child process with no shell and no file tools; its two extension tools (`web_open`, `escalate`) POST here. Each
run gets a random token that is bound to one task, its allowed domains, a call budget and a deadline, and is revoked when
the run ends. Pages are opened in the task's own (headless) browser over CDP, and everything that comes back passes through
the injection gateway and is wrapped as untrusted data. The server never touches the vault.
"""

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.security.gateway import inspect_page_text, url_in_plan, wrap_untrusted
from app.sites.loader import hints_for, looks_logged_out
from app.telemetry import tel

log = logging.getLogger("nami.harness")
MAX_PAGE_CHARS = 9_000                  # what the model sees of one page
MAX_KEPT_CHARS = 24_000                 # what we keep across the run to check the final answer against
MAX_LINKS = 40
LOOPBACK = {"127.0.0.1", "::1"}


@dataclass
class Visit:
    url: str
    title: str
    text: str                            # gateway-cleaned, capped


@dataclass
class RunCtx:
    task_id: str
    domains: list[str]
    cdp_url: str
    deadline: float
    max_calls: int
    calls: int = 0
    visits: list[Visit] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)        # off-plan attempts and similar, by kind (never page text)
    flagged: list[str] = field(default_factory=list)        # injection excerpts the gateway removed
    escalate: str | None = None                             # why the agent says it cannot go on (login wall, CAPTCHA, form)
    kept: int = 0


def _err(msg: str, status: int = 200) -> JSONResponse:
    return JSONResponse({"ok": False, "text": msg}, status_code=status)


class ToolServer:
    def __init__(self) -> None:
        self._runs: dict[str, RunCtx] = {}
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None
        self.port: int | None = None

    def build_app(self) -> Starlette:
        return Starlette(routes=[Route("/{token}/open", self._open, methods=["POST"]),
                                 Route("/{token}/escalate", self._escalate, methods=["POST"])])

    # ── lifecycle ──
    async def ensure_started(self) -> int:
        if self._server is not None and self.port:
            return self.port
        server = uvicorn.Server(uvicorn.Config(self.build_app(), host="127.0.0.1", port=0, log_level="warning", lifespan="off"))
        self._task = asyncio.create_task(server.serve())
        for _ in range(100):
            if server.started and server.servers:
                break
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError("the exploration tool server did not start")
        self._server, self.port = server, server.servers[0].sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
            if self._task:
                await asyncio.wait([self._task], timeout=5)
        self._server, self._task, self.port = None, None, None

    def register(self, ctx: RunCtx) -> str:
        token = secrets.token_urlsafe(32)
        self._runs[token] = ctx
        return token

    def revoke(self, token: str) -> None:
        self._runs.pop(token, None)

    # ── requests ──
    def _ctx(self, request: Request) -> RunCtx | None:
        if request.client is None or request.client.host not in LOOPBACK:
            return None
        return self._runs.get(request.path_params["token"])

    async def _open(self, request: Request) -> JSONResponse:
        ctx = self._ctx(request)
        if ctx is None:
            return _err("not found", 404)
        try:
            body = await request.json()
            url = str(body.get("url", ""))[:2000]
        except Exception:
            return _err("bad request", 400)
        if time.monotonic() > ctx.deadline or ctx.calls >= ctx.max_calls:
            return _err("The page budget for this task is used up. Answer now from what you have already read.")
        ctx.calls += 1
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or not url_in_plan(url, ctx.domains):
            ctx.blocked.append("off_plan_url")
            tel.inc("gateway_blocks_total", layer="L5")
            return _err(f"Not allowed. You may only open pages on: {', '.join(ctx.domains)}.")
        try:
            return await self._fetch(ctx, url)
        except Exception as e:                                   # a page that will not load is a normal event, not a crash
            log.info("explore open failed: %s", type(e).__name__)
            return _err(f"The page could not be opened ({type(e).__name__}). Try another page or answer from what you have.")

    async def _fetch(self, ctx: RunCtx, url: str) -> JSONResponse:
        from app.browser import cdp
        async with cdp.connect(ctx.cdp_url) as browser:
            page = await browser.contexts[0].new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=4_000)       # SPAs: give them a moment, never wait long
                except Exception:
                    pass
                final = page.url
                if not url_in_plan(final, ctx.domains):            # a redirect left the plan: read nothing from it
                    ctx.blocked.append("off_plan_redirect")
                    tel.inc("gateway_blocks_total", layer="L5")
                    return _err("That page redirected outside the allowed sites, so I did not read it.")
                title = (await page.title())[:200]
                has_password = await page.evaluate("() => !!document.querySelector('input[type=password]')")
                text = (await page.evaluate("() => document.body ? document.body.innerText : ''"))[:40_000]
                anchors = await page.evaluate(
                    "() => Array.from(document.querySelectorAll('a[href]')).slice(0, 400).map(a => [(a.innerText||'').trim().slice(0,80), a.href])")
            finally:
                await page.close()
        site = urlparse(final).hostname or ""
        if has_password or looks_logged_out(final, hints_for(site)):
            ctx.escalate = ctx.escalate or "login_required"
            return _err("This page asks for a sign-in. Do not try to sign in. Call escalate with a short reason.")
        verdict = inspect_page_text(text, max_len=MAX_PAGE_CHARS)
        if verdict.suspicious:
            tel.inc("gateway_blocks_total", layer="L2")
            ctx.flagged += [h.excerpt for h in verdict.hits][:3]
        keep = verdict.text[: max(0, MAX_KEPT_CHARS - ctx.kept)]
        ctx.kept += len(keep)
        ctx.visits.append(Visit(final, title, keep))
        seen, links = set(), []
        for label, href in anchors:
            if href not in seen and url_in_plan(href, ctx.domains):         # only links that stay on the allowed sites
                seen.add(href)
                links.append(f"- {label or '(no text)'} -> {href}")
            if len(links) >= MAX_LINKS:
                break
        out = f"URL: {final}\nTitle: {title}\n\n{wrap_untrusted(verdict.text)}\n\nLinks on allowed sites:\n" + "\n".join(links)
        return JSONResponse({"ok": True, "text": out})

    async def _escalate(self, request: Request) -> JSONResponse:
        ctx = self._ctx(request)
        if ctx is None:
            return _err("not found", 404)
        try:
            reason = str((await request.json()).get("reason", ""))[:200]
        except Exception:
            reason = ""
        ctx.escalate = ctx.escalate or (reason or "needs_user")
        return JSONResponse({"ok": True, "text": "Noted. Stop now and reply with one short sentence."})
