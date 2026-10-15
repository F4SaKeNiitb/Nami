"""FastAPI entry point: Chainlit UI at /chainlit, vault API at /chainlit/vault, health at /healthz."""

import asyncio
import logging
from contextlib import asynccontextmanager

from chainlit.utils import mount_chainlit
from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.chainlit_schema import ensure_chainlit_schema
from app.admin import router as admin_router
from app.core.cleanup import cleanup_loop, idle_browser_loop
from app.core.runtime import start_runtime
from app.skills.router import router as skills_router
from app.tasks_router import router as tasks_router
from app.skills.service import nightly_reverify
from app.vault.router import router as vault_router
from app.browser.live_router import router as live_router
from app.connect.router import router as oauth_router
from app.slack.app import start_slack
from app.slack.router import HideQuery, router as slack_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("uvicorn.access").addFilter(HideQuery())


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_chainlit_schema()
    rt = await start_runtime()
    stop = asyncio.Event()
    job = asyncio.create_task(nightly_reverify(rt.orchestrator.skills, stop=stop))
    sweep = asyncio.create_task(cleanup_loop(rt.orchestrator.api.out_dir, rt.settings.generated_ttl_hours, stop=stop))
    idle = asyncio.create_task(idle_browser_loop(rt.orchestrator, stop=stop))
    slack = None
    try:
        slack = await start_slack(rt)
    except Exception:
        logging.getLogger("nami.slack").exception("Slack bot did not start; the web app keeps running")
    try:
        yield
    finally:
        if slack:
            await slack.stop()
        stop.set()
        await job
        await sweep
        await idle
        await rt.close()


app = FastAPI(title="Nami", lifespan=lifespan)
app.include_router(admin_router)
app.include_router(skills_router)
app.include_router(tasks_router)          # /chainlit/tasks/* (ledger read, cancel), same reason
app.include_router(vault_router)          # registered before the Chainlit mount so /chainlit/vault/* wins
app.include_router(live_router)           # /chainlit/live/ws/* (in-page sign-in browser), same reason
app.include_router(slack_router)          # /chainlit/slack/add and /solve (one-time pages)
app.include_router(oauth_router)          # /chainlit/connect/* (Connect Google), same reason


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/.well-known/jwks.json")
async def jwks():
    from app.core.runtime import runtime
    return runtime().identity.issuer.jwks()


@app.get("/")
async def root():
    return RedirectResponse("/chainlit")


mount_chainlit(app=app, target="app/cl_app.py", path="/chainlit")
