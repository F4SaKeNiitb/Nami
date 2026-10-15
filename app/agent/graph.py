"""The orchestrator: intent -> access -> plan -> (gate -> run)* -> verify -> remember -> finalize.

LangGraph re-runs a node from its first line when it resumes from `interrupt()`. So every pause is its
own tiny node that does nothing but `interrupt()`; browser work always happens in nodes BEFORE one.
Live objects (browser sessions) are held in `self._live`, never in graph state, so state stays
serialisable for the SQLite checkpointer; after a crash `_ensure_browser` simply reopens.
"""

import base64
import logging
import re
import asyncio
import time
from urllib.parse import urlparse
from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.agent.access_planner import (api_credentials, wants_api_route, build_access_plan, credential_ask, google_credentials, google_sso_login,
                                      is_google_workspace, usable_credentials, with_google)
from app.agent.context import asks_for_secret
from app.agent.guard import Gate, Guard
from app.agent.intent import SecretInRequest, parse_intent
from app.agent.planner import make_plan, sign_in_subgoal
from app.agent.suggest import suggestion_text
from app.agent.schemas import AccessPlan, ApiChoice, Intent, Plan, Subgoal
from app.agent.validator import validate
from app.agent.verifier import UNVERIFIED_BELOW, verify
from app.browser import api_auth
from app.browser.api_executor import ApiError, ApiExecutor, check_params
from app.browser.broker import Broker
from app.browser.executor import ONE_TIME_NOTE, Executor, observe
from app.browser.live_relay import LiveRelay
from app.connect.google_api import GoogleApiError, GoogleWorkspace, make_draft
from app.connect.google_oauth import GoogleOAuth, OAuthError
from app.agent.google_runner import run_google
from app.harness.pi_runner import PiHarness
from app.harness.router import explore_eligible
from app.browser.provider import BrowserProvider, BrowserSession
from app.browser.session_manager import SessionManager
from app.cache.llm_cache import LLMCacheScope
from app.core.config import Settings
from app.core.container import Services
from app.core.llm import LLM, LLMError
from app.memory.commands import memory_block
from app.db.base import new_id
from app.db.models import Task, TaskEvent
from app.security.authz import Resource
from app.skills.judge import judge
from app.skills.schemas import Manifest, SkillContent
from app.skills.service import SkillService
from app.security.classifier import inspect_page_text_async
from app.security.gateway import inspect_page_text, url_in_plan
from app.security.secrets import find_secrets, redact
from app.sites.loader import start_urls, canonical_site, generation_sites, hints_for
from app.sites.policy import needs_approval
from app.telemetry import current_task, tel
from app.tenancy.service import PermissionDenied
from app.vault.origins import registrable_domain
from app.vault.service import VaultError, VaultService

log = logging.getLogger("nami.graph")
Emit = Callable[[str, dict], Awaitable[None]]
MAX_RETRIES, MAX_REPLANS = 2, 1
# A sign-in that ran out of time, or whose page simply did not show the result yet, is not evidence the site refused the login (found live: 4 of 5
# "login rejected" messages arrived while the agent was still working). Those get an ordinary retry first.
_NOT_A_REJECTION = re.compile(r"took longer|did not show|ran out|maximum steps|ended without|budget", re.I)
_ABOUT_A_CODE = re.compile(r"\botp\b|one[- ]?time|verification code|\bcode\b", re.I)
MAX_CONNECT_RETRIES = 3
_CODE_RE = re.compile(r"[0-9A-Za-z]{4,8}")


class State(TypedDict, total=False):
    task_id: str
    user_id: str
    org_id: str
    thread_id: str | None
    request: str
    history: str
    memories: list[str]      # long-term memories recalled for this request (the user's own earlier words), redacted
    clarified: bool
    intent: dict
    access: dict
    plan: dict
    idx: int
    evidence: list[str]
    connect_retries: int
    ask_retries: int         # times the user said "added it" but nothing usable for this site was saved
    code_asks: int           # times this task asked for a verification code
    totp_retries: int        # one bounded retry using a newly generated saved authenticator code
    session_reauths: int     # automatic session recovery attempts; prevents repeated sign-in insertion loops
    agent_asks: int          # questions the browser agent put to the user (ask_user)
    confirm_asks: int        # approvals the browser agent requested on a step the planner did not tag (confirm_action)
    answers: list[str]       # the user's answers to those questions: redacted data, shown to the next run as untrusted notes
    known_creds: list[str]   # ids of the credentials saved when we last asked (ids only), to tell what is new
    claims: list[str]        # executor reports that the validator accepted, redacted; handed to the verifier as untrusted text
    approved: list[int]
    attempts: int
    replans: int
    reflexion: str
    healthy: bool
    suspicious: list[str]
    amount: float | None
    currency: str | None
    pending: dict | None
    resume: Any
    final: dict | None
    route: str
    observed: list[str]
    skills_used: list[str]
    last_claim: str          # the executor's latest report (redacted), shown if the budget runs out before it is accepted
    explored: bool           # the answer came from the pi exploration route (verified against the pages it read)
    api_endpoint: str
    api_auth_candidates: list  # ways to send the saved key still to be tried (header names and the like, never a secret)
    api_params: dict         # validated params of an approved generation (the user's own words; never a secret)
    api_media: list          # files/links a generation produced: [{"kind", "path"|"url"}]
    draft: dict              # the email draft shown on the approval card; only this exact draft is ever sent
    api_url: str
    api_text: str
    api_content: str         # the full generated text, shown to the user as the answer
    just_connected: str      # site that was connected during this task (the result then ends with next-step suggestions)


def observe_flags(sg: Subgoal) -> dict:
    """What the validator may look at after a step. A sign-in step is judged on the page TEXT (redacted of every held value) but never
    on a screenshot, which could show a typed username or code. With no evidence at all the validator can never confirm a sign-in,
    so every saved-password login looked rejected (found in the first live run)."""
    return {"with_text": True, "with_shot": not sg.is_auth}


class Outcome:
    """What the UI gets back after each run of the graph: either a pause to show, or a final result."""

    def __init__(self, task_id: str, pending: dict | None, final: dict | None):
        self.task_id, self.pending, self.final = task_id, pending, final


def _scrub(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items() if not isinstance(v, bytes)}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


def _warning(excerpts: list[str] | None) -> str:
    if not excerpts:
        return ""
    return ("A page contained text that tried to give me instructions. I ignored it, but look before approving: "
            + " | ".join(excerpts[:2]))


class Orchestrator:
    def __init__(self, *, services: Services, vault: VaultService, sessions: SessionManager, provider: BrowserProvider,
                 llm: LLM, executor: Executor, settings: Settings, checkpointer: BaseCheckpointSaver, identity=None,
                 skills: SkillService | None = None, api: ApiExecutor | None = None,
                 google_oauth: GoogleOAuth | None = None, google_api: GoogleWorkspace | None = None,
                 harness: PiHarness | None = None):
        self.svc, self.vault, self.sessions, self.provider = services, vault, sessions, provider
        self.llm, self.executor, self.s = llm, executor, settings
        self.identity = identity
        if hasattr(sessions, "health_ttl"):
            sessions.health_ttl = float(settings.health_ttl_seconds)
        self.skills = skills
        self.api = api or ApiExecutor()
        self.google_oauth = google_oauth or GoogleOAuth(client_id=None, client_secret=None, signing_key="unset")
        self.google_api = google_api or GoogleWorkspace()
        self.harness = harness                    # the pi exploration harness; None or unavailable means the deterministic path only
        self._live: dict[str, BrowserSession] = {}
        self._headless: dict[str, bool] = {}      # task_id -> whether its live browser has no window
        self._budget: dict[str, dict] = {}        # task_id -> {steps, t0}; in memory like _live, so a restart starts a fresh budget
        self._emit: dict[str, Emit] = {}
        self._shots: dict[str, bytes] = {}               # task_id -> the last page screenshot, shown on an approval card. Memory only: never in state, DB or events
        self._asked_at: dict[str, float] = {}            # task_id -> when it paused to wait for the user (their time is not the task's time)
        self._touched: dict[str, float] = {}             # task_id -> when its browser was last used (monotonic). Memory only
        self._driving: dict[str, str] = {}               # task_id -> user, for tasks running right now (not those waiting on the user): the per-user cap
        self._busy: set[str] = set()                     # tasks with a graph step in flight right now: their browser is never idle
        self._resume_url: dict[str, str] = {}            # task_id -> page it was on when its idle browser was closed
        self._codes: dict[str, tuple[str, float]] = {}   # task_id -> (one-time code, expiry). Memory only: never in state, DB, events or logs
        self.live_relay = LiveRelay()            # in-page live view for browsers that have none of their own
        self.graph = self._build().compile(checkpointer=checkpointer)

    # ───────────────────────── public API ─────────────────────────

    async def start(self, *, user_id: str, org_id: str, thread_id: str | None, text: str, history: str,
                    emit: Emit, task_id: str | None = None) -> Outcome:
        """`task_id`: a surface may pick the id up front (a fresh one from `new_id()`), so it can cancel the run while it is still going."""
        p = await self.svc.authz.principal(user_id, org_id)
        d = await self.svc.authz.check(p, "start_task", Resource("org", org_id, id=org_id))
        if not d.allow:
            return Outcome("", None, {"kind": "blocked", "message": f"I can't start that for you: {d.reason}."})
        cap = self.s.max_tasks_per_user
        if cap and sum(1 for u in self._driving.values() if u == user_id) >= cap:
            return Outcome("", None, {"kind": "blocked", "message": f"You already have {cap} tasks running. Let one finish (or stop it), then ask again."})
        task_id = task_id or new_id()
        async with self.svc.db.session() as s:
            s.add(Task(id=task_id, org_id=org_id, user_id=user_id, thread_id=thread_id, request_text=redact(text)))
        self._emit[task_id] = emit
        init: State = {"task_id": task_id, "user_id": user_id, "org_id": org_id, "thread_id": thread_id, "request": text,
                       "history": history, "clarified": False, "idx": 0, "evidence": [], "approved": [], "attempts": 0,
                       "totp_retries": 0,
                       "session_reauths": 0,
                       "replans": 0, "reflexion": "", "healthy": False, "pending": None, "final": None}
        return await self._drive(task_id, init, emit)

    async def _owns(self, task_id: str, user_id: str, action: str) -> bool:
        """Only the task's owner, in the task's organisation, in a role that may act. Missing and foreign look the same."""
        async with self.svc.db.session() as s:
            t = await s.get(Task, task_id)
            owner, org = (t.user_id, t.org_id) if t else (None, "")
        p = await self.svc.authz.principal(user_id, org)
        return (await self.svc.authz.check(p, action, Resource("task", org, owner, id=task_id))).allow and owner is not None

    async def resume(self, task_id: str, value: Any, emit: Emit, *, user_id: str) -> Outcome:
        if not await self._owns(task_id, user_id, "resume_task"):
            return Outcome(task_id, None, {"kind": "blocked", "message": "I can't find that task."})
        self._emit[task_id] = emit
        self._credit_wait(task_id)
        return await self._drive(task_id, Command(resume=value), emit)

    async def cancel(self, task_id: str, *, user_id: str) -> None:
        if not await self._owns(task_id, user_id, "cancel_task"):
            raise PermissionDenied("not your task")
        self._budget.pop(task_id, None)
        await self._cleanup(task_id, capture=False)
        await self._set_status(task_id, "cancelled")

    async def recover_on_boot(self) -> int:
        """Tasks that were mid-flight when the process died become 'blocked' (interrupted), resumable by their owner."""
        from sqlalchemy import update
        async with self.svc.db.session() as s:
            res = await s.execute(update(Task).where(Task.status.in_(
                ["planning", "awaiting_access", "running", "awaiting_user", "awaiting_approval", "verifying"]))
                .values(status="blocked"))
            return res.rowcount or 0

    async def interrupted(self, user_id: str) -> list[tuple[str, str]]:
        """(task_id, request) for this user's tasks that stopped without a final result."""
        from sqlalchemy import select
        async with self.svc.db.session() as s:
            rows = await s.execute(select(Task.id, Task.request_text).where(
                Task.user_id == user_id, Task.status == "blocked", Task.result.is_(None)).order_by(Task.created_at.desc()).limit(3))
            return [(r[0], r[1]) for r in rows]

    async def paused_in_thread(self, user_id: str, thread_id: str) -> tuple[str, dict] | None:
        """(task_id, card) for this user's task in this conversation that is waiting on them (an approval, a code, a login, a
        question). Read only: nothing is run or changed. Only a task with a saved pause counts; one that crashed mid-step is not
        one, so a chat surface can show the card again after a restart without ever re-running a step on its own."""
        from sqlalchemy import select
        async with self.svc.db.session() as s:
            ids = (await s.scalars(select(Task.id).where(
                Task.user_id == user_id, Task.thread_id == thread_id, Task.result.is_(None),
                Task.status.in_(["blocked", "awaiting_access", "awaiting_user", "awaiting_approval"])).order_by(Task.created_at.desc()).limit(3))).all()
        for tid in ids:
            if pending := await self._pending(tid):
                return tid, pending
        return None

    async def resume_interrupted(self, task_id: str, emit: Emit, *, user_id: str) -> Outcome:
        """Continue from the last checkpoint. A paused card is shown again; a crashed step is re-run."""
        if not await self._owns(task_id, user_id, "resume_task"):
            return Outcome(task_id, None, {"kind": "blocked", "message": "I can't find that task."})
        self._emit[task_id] = emit
        cfg = {"configurable": {"thread_id": task_id}}
        snap = await self.graph.aget_state(cfg)
        if not snap.values:
            await self._set_status(task_id, "cancelled")
            return Outcome(task_id, None, {"kind": "error", "message": "I lost track of that task. Please ask again."})
        for t in snap.tasks:
            if t.interrupts:
                pending = t.interrupts[0].value
                if pending.get("type") == "takeover" and not pending.get("offer_only") and task_id not in self._live:
                    live = await self._ensure_browser(snap.values, headless=self._in_page())   # fresh browser from the saved profile
                    await self._open_login_page(live, hints_for(pending["site"]).auth_check_url or f"https://{pending['site']}")
                    pending = {**pending, "live_view_url": live.live_view_url, "in_page": self._shows_in_page(live)}
                await self._set_status(task_id, {"approval": "awaiting_approval", "need_credentials": "awaiting_access"}.get(
                    pending["type"], "awaiting_user"))
                return Outcome(task_id, pending, None)
        await self._set_status(task_id, "running")
        return await self._drive(task_id, None, emit)

    async def _pending(self, task_id: str) -> dict | None:
        snap = await self.graph.aget_state({"configurable": {"thread_id": task_id}})
        return next((t.interrupts[0].value for t in snap.tasks if t.interrupts), None)

    async def offer_code(self, task_id: str, user_id: str, code: str) -> bool:
        """Hold a verification code the user just typed, in memory only, for the next run of this task. Accepted only from the
        task's owner, only while the task is actually waiting for a code, and only if it looks like a code. It is never written
        to the database, an event, a log or the graph state, expires, and is used once."""
        code = (code or "").strip()
        if not _CODE_RE.fullmatch(code) or not await self._owns(task_id, user_id, "resume_task"):
            return False
        pending = await self._pending(task_id)
        if not pending or pending.get("purpose") != "code":
            return False
        self._codes[task_id] = (code, time.monotonic() + self.s.otp_code_ttl_seconds)
        return True

    async def approval_image(self, task_id: str, user_id: str) -> bytes | None:
        """The page the user is being asked to approve, for the approval card. Owner only, and only while that approval is pending.
        Held in memory (a restart just means a card without a picture). Never sent to a model, stored, or put in an event."""
        if not await self._owns(task_id, user_id, "resume_task"):
            return None
        pending = await self._pending(task_id)
        return self._shots.get(task_id) if pending and pending.get("type") == "approval" else None

    def _take_code(self, task_id: str) -> str | None:
        item = self._codes.pop(task_id, None)
        return item[0] if item and time.monotonic() <= item[1] else None

    async def surface(self, task_id: str, user_id: str) -> dict | None:
        """The one explicit way a browser reaches the user: they clicked "Sign in once" on a card that offered it."""
        if not await self._owns(task_id, user_id, "resume_task"):
            return None
        pending = await self._pending(task_id)
        if not pending or pending.get("type") != "takeover" or not pending.get("offer_only"):
            return None
        snap = await self.graph.aget_state({"configurable": {"thread_id": task_id}})
        s, site = snap.values, pending["site"]
        fresh = task_id not in self._live
        live = await self._ensure_browser(s, headless=self._in_page())
        if fresh:
            await self._open_login_page(live, hints_for(site).auth_check_url or f"https://{site}")
        live = await self._surface_for_user(s, live)
        if self._shows_in_page(live):
            await self._event(task_id, "browser_surfaced", {"site": site})
        return {"live_view_url": live.live_view_url, "in_page": self._shows_in_page(live)}

    async def _drive(self, task_id: str, payload: Any, emit: Emit) -> Outcome:
        cfg = {"configurable": {"thread_id": task_id}}
        self._driving[task_id] = (payload.get("user_id") if isinstance(payload, dict) else None) or self._driving.get(task_id) or await self._owner_of(task_id)
        try:
            await self.graph.ainvoke(payload, cfg)
        except Exception as e:
            log.exception("graph crashed")
            await self._cleanup(task_id, capture=False)
            await self._set_status(task_id, "failed")
            return Outcome(task_id, None, {"kind": "error", "message": redact(f"{type(e).__name__}: {e}")[:300]})
        finally:
            self._driving.pop(task_id, None)
        snap = await self.graph.aget_state(cfg)
        for t in snap.tasks:
            if t.interrupts:
                self._asked_at[task_id] = time.monotonic()
                return Outcome(task_id, t.interrupts[0].value, None)
        return Outcome(task_id, None, snap.values.get("final"))

    async def _owner_of(self, task_id: str) -> str:
        async with self.svc.db.session() as s:
            t = await s.get(Task, task_id)
        return t.user_id if t else ""

    def _credit_wait(self, task_id: str) -> None:
        """The user answered: the time they took does not count against the task's time budget (found live: a task ended with "I stopped after
        10 minutes" mostly made of waiting for a verification code)."""
        asked = self._asked_at.pop(task_id, None)
        if asked is not None and task_id in self._budget:
            self._budget[task_id]["waited"] = self._budget[task_id].get("waited", 0.0) + (time.monotonic() - asked)

    # ───────────────────────── helpers ─────────────────────────

    async def _event(self, state_or_id: State | str, type_: str, payload: dict) -> None:
        task_id = state_or_id if isinstance(state_or_id, str) else state_or_id["task_id"]
        clean = _scrub(payload)
        try:
            async with self.svc.db.session() as s:
                s.add(TaskEvent(task_id=task_id, type=type_, payload=clean))
        except Exception:
            log.exception("event persist failed")
        if emit := self._emit.get(task_id):
            try:
                await emit(type_, payload)         # UI gets the full payload (it may include a screenshot)
            except Exception:
                log.debug("emit failed", exc_info=True)

    async def _set_status(self, task_id: str, status: str, result: dict | None = None) -> None:
        async with self.svc.db.session() as s:
            t = await s.get(Task, task_id)
            if t:
                t.status = status
                if result is not None:
                    t.result = _scrub(result)

    @staticmethod
    def _access(s: State) -> AccessPlan:
        return AccessPlan.model_validate(s["access"])

    @staticmethod
    def _plan(s: State) -> Plan:
        return Plan.model_validate(s["plan"])

    def _guard(self, s: State) -> Guard:
        g = Guard(self._access(s))
        for i in s.get("approved", []):
            g.approve(i)
        return g

    async def _ensure_browser(self, s: State, *, headless: bool) -> BrowserSession:
        tid = s["task_id"]
        live = self._live.get(tid)
        self._touched[tid] = time.monotonic()
        if live:
            return live
        # One profile has one writer: the user's first browser holds it, and their other tasks get their own profile-less browsers
        # (the saved logins are restored from the encrypted snapshots below), so several tasks can run at once.
        live = await self.provider.open(s["user_id"], headless=headless, scratch_if_busy=True)
        self._live[tid] = live
        self._headless[tid] = headless
        for site in self._access(s).allowed_domains:        # restore saved logins before anything runs
            try:
                await self.sessions.restore(s["user_id"], live.cdp_url, site)
            except Exception:
                log.warning("snapshot restore failed for %s", site, exc_info=True)
        if url := self._resume_url.pop(tid, None):          # the browser was closed for being idle: come back to the same page
            await self._open_login_page(live, url)
        return live

    async def _surface_for_user(self, s: State, live: BrowserSession) -> BrowserSession:
        """The user must be able to SEE the browser when asked to take over. A headless local browser has no window, so
        reopen the same profile with a window on the page the agent was on. Hosted browsers (live view URL) are left alone."""
        tid = s["task_id"]
        if live.live_view_url or not self._headless.get(tid) or self._in_page():
            return live                                     # in-page mode: the user sees it inside the chat, no window
        try:
            url, _, _ = await observe(live.cdp_url, "", with_text=False, with_shot=False)
        except Exception:
            url = ""
        await self._close_browser(tid)                      # Browser.close flushes the profile, so the login survives the reopen
        live = await self._ensure_browser(s, headless=False)
        target = url if url.startswith(("http://", "https://")) and url_in_plan(url, self._access(s).allowed_domains) else \
            f"https://{self._access(s).allowed_domains[0]}"
        await self._open_login_page(live, target)
        await self._event(s, "browser_surfaced", {"url": target})
        return live

    def _in_page(self) -> bool:
        return self.s.takeover_view == "inpage"

    def _shows_in_page(self, live: BrowserSession) -> bool:
        return self._in_page() and not live.live_view_url

    def live_ticket(self, task_id: str, user_id: str) -> str | None:
        """A fresh, short-lived viewer ticket for this task's browser, issued when the chat card is drawn. Never stored in
        the checkpoint or the event log. Only the task's own user gets one, and only for a browser with no live view."""
        live = self._live.get(task_id)
        if live is None or live.live_view_url or live.user_id != user_id or not self._in_page():
            return None
        return self.live_relay.issue(user_id=user_id, task_id=task_id, cdp_url=live.cdp_url)

    async def _close_browser(self, task_id: str) -> None:
        await self.live_relay.revoke_task(task_id)          # any open in-page viewer closes with the browser
        self._headless.pop(task_id, None)
        self._touched.pop(task_id, None)
        live = self._live.pop(task_id, None)
        if live:
            await self.provider.close(live)

    async def reap_idle(self, idle_seconds: float | None = None) -> int:
        """Close browsers nobody has used for `browser_idle_seconds` (default 10 minutes): a task waiting on an approval or a code, or a
        browser left open after its work. A browser with a step in flight is never touched. The page it was on is remembered so a
        resume opens the same page; the profile is saved by the close itself. Returns how many were closed."""
        limit = self.s.browser_idle_seconds if idle_seconds is None else idle_seconds
        now, closed = time.monotonic(), 0
        for tid in list(self._live):
            if tid in self._busy or now - self._touched.get(tid, now) < limit:
                continue
            try:
                url, _, _ = await asyncio.wait_for(observe(self._live[tid].cdp_url, "", with_text=False, with_shot=False), 8)
                if url.startswith("http"):
                    self._resume_url[tid] = url
            except Exception:
                pass                                         # a dead or unreachable browser is closed all the same
            log.info("closing the browser of task %s after %ds idle", tid, int(now - self._touched.get(tid, now)))
            try:
                await self._close_browser(tid)
                closed += 1
            except Exception:
                log.warning("closing an idle browser failed", exc_info=True)
        return closed

    async def _cleanup(self, task_id: str, *, capture: bool, state: State | None = None) -> None:
        self._codes.pop(task_id, None)
        self._shots.pop(task_id, None)
        self._asked_at.pop(task_id, None)
        self._resume_url.pop(task_id, None)
        self._touched.pop(task_id, None)
        live = self._live.get(task_id)
        if live and capture and state:
            for site in self._access(state).allowed_domains:
                try:
                    if state["access"]["login"] != "none":
                        await self.sessions.capture(state["user_id"], live.cdp_url, site)
                except Exception:
                    log.warning("snapshot capture failed for %s", site, exc_info=True)
        await self._close_browser(task_id)

    # ───────────────────────── graph ─────────────────────────

    def _traced(self, name: str, fn):
        """One span per graph node, tagged org/user/task. Pauses re-run a node from its start, so spans are cheap and flat."""
        async def wrapper(s: State):
            current_task.set(s.get("task_id"))
            tid = s.get("task_id")
            self._busy.add(tid)
            try:
                with tel.span(f"graph.{name}", org_id=s.get("org_id"), user_id=s.get("user_id"), task_id=tid,
                              **{"graph.idx": s.get("idx", 0)}) as sp:
                    out = await fn(s)
                    if sp is not None and isinstance(out, dict) and out.get("route"):
                        sp.set_attribute("graph.route", out["route"])
                    return out
            finally:                                         # also when the step pauses (interrupt) or fails: the idle clock starts now
                self._busy.discard(tid)
                self._touched[tid] = time.monotonic()
        wrapper.__name__ = fn.__name__
        return wrapper

    def _build(self) -> StateGraph:
        g = StateGraph(State)
        for name in ("intent", "clarify", "access", "explore", "ask_credentials", "credentials_check", "connect_prepare", "connect_wait", "connect_finish", "plan",
                     "gate", "approve", "run", "pause", "verify", "remember", "finalize"):
            g.add_node(name, self._traced(name, getattr(self, f"n_{name}")))
        route = lambda s: s["route"]        # noqa: E731
        g.add_edge(START, "intent")
        g.add_conditional_edges("intent", route, {"clarify": "clarify", "access": "access", "finalize": "finalize"})
        g.add_edge("clarify", "intent")
        g.add_conditional_edges("access", route, {"ask": "ask_credentials", "plan": "plan", "explore": "explore",
                                                      "finalize": "finalize"})
        g.add_conditional_edges("explore", route, {"verify": "verify", "plan": "plan"})
        g.add_edge("ask_credentials", "credentials_check")
        g.add_conditional_edges("credentials_check", route, {"access": "access", "run": "run", "connect": "connect_prepare",
                                                             "ask": "ask_credentials", "finalize": "finalize"})
        g.add_edge("connect_prepare", "connect_wait")
        g.add_edge("connect_wait", "connect_finish")
        g.add_conditional_edges("connect_finish", route, {"plan": "plan", "run": "run", "finalize": "finalize", "connect_retry": "connect_wait"})
        g.add_conditional_edges("plan", route, {"gate": "gate", "finalize": "finalize"})
        g.add_conditional_edges("gate", route, {"approve": "approve", "run": "run", "verify": "verify", "approve_blocked": "finalize"})
        g.add_conditional_edges("approve", route, {"run": "run", "finalize": "finalize"})
        g.add_conditional_edges("run", route, {"gate": "gate", "run": "run", "approve": "approve", "pause": "pause",
                                                "ask": "ask_credentials", "verify": "verify", "finalize": "finalize"})
        g.add_conditional_edges("pause", route, {"run": "run", "finalize": "finalize"})
        g.add_edge("verify", "remember")
        g.add_edge("remember", "finalize")
        g.add_edge("finalize", END)
        return g

    # ── 1. intent ──
    async def _recall(self, s: State) -> list[str]:
        """Memories relevant to this request. Only the user's own explicit 'remember ...' lines are ever stored, so this is
        user-level text, not page content. A memory failure must never stop a task."""
        if "memories" in s:                       # recalled once per task; a resume or a clarification reuses it
            return s["memories"]
        try:
            found = await self.svc.ltm.recall(org_id=s["org_id"], user_id=s["user_id"], query=s["request"],
                                              kinds={"fact", "preference"}, k=self.s.ltm_task_top_k)
        except Exception:
            log.warning("long-term memory recall failed; continuing without it", exc_info=True)
            return []
        return [redact(m.content) for m in found]

    async def n_intent(self, s: State) -> State:
        await self._set_status(s["task_id"], "planning")
        memories = await self._recall(s)
        if memories and "memories" not in s:
            await self._event(s, "memory_recalled", {"count": len(memories)})
        cached = None
        scope = None
        if not s.get("history") and not s.get("clarified") and not memories:   # only context-free requests are cacheable
            rules = await self.svc.rules.resolve(org_id=s["org_id"], user_id=s["user_id"])
            scope = LLMCacheScope(s["org_id"], s["user_id"], self.s.model_fast, "intent", rules_version=rules.version)
            cached = await self.svc.llm_cache.lookup(scope, s["request"])
        try:
            if cached:
                intent = Intent.model_validate(cached.response)
            else:
                intent = await parse_intent(self.llm, s["request"], history=s.get("history", ""),
                                            memory=memory_block(memories), model=self.s.model_fast,
                                            second_opinion_model=self.s.model_strong)
                if scope and not intent.clarify and not intent.unsupported:      # a refusal is never replayed from the cache
                    await self.svc.llm_cache.store(scope, s["request"], intent.model_dump())
        except SecretInRequest:
            return {"route": "finalize", "final": {"kind": "secret_refused"}}
        except LLMError as e:
            return {"route": "finalize", "final": {"kind": "error", "message": str(e)[:200]}}
        out: State = {"intent": intent.model_dump(), "memories": memories}
        if intent.unsupported:
            return {**out, "route": "finalize", "final": {"kind": "unsupported", "message": intent.unsupported}}
        if intent.clarify and not s.get("clarified"):
            return {**out, "route": "clarify", "pending": {"type": "ask_user", "question": intent.clarify}}
        await self._event(s, "intent", {"goal": intent.goal, "sites": intent.sites,
                                         "cache": cached.kind if cached else None})
        return {**out, "route": "access", "pending": None}

    async def n_clarify(self, s: State) -> State:
        answer = interrupt(s["pending"])
        return {"request": f"{s['request']}\n(User clarified: {answer})", "clarified": True, "pending": None}

    @staticmethod
    async def _open_login_page(live: BrowserSession, url: str) -> None:
        try:
            from app.browser import cdp
            async with cdp.connect(live.cdp_url) as b:
                page = await b.contexts[0].new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        except Exception:
            log.warning("could not open %s for takeover", url, exc_info=True)

    # ── 2. access plan (code, from user text only) ──
    async def n_access(self, s: State) -> State:
        intent = Intent.model_validate(s["intent"])
        workspace = is_google_workspace(intent)                  # judged on the user's words, before gmail.com -> google.com
        intent = intent.model_copy(update={"sites": list(dict.fromkeys(canonical_site(x) for x in intent.sites))})   # gmail.com -> google.com
        creds = await self.vault.list(s["user_id"])
        if intent.generate and not intent.sites:                  # "make an image of ...": use the one generation service they saved a key for
            rules0 = await self.svc.rules.resolve(org_id=s["org_id"], user_id=s["user_id"])
            have = [g for g in generation_sites() if api_credentials(g, creds, rules0)]
            if len(have) == 1:
                intent = intent.model_copy(update={"sites": have, "needs_login": True})
        if not intent.needs_login and wants_api_route(intent, s["request"]):      # use the key (or ask for it), never the website
            intent = intent.model_copy(update={"needs_login": True})
        site = intent.sites[0] if intent.sites else None
        rules = await self.svc.rules.resolve(org_id=s["org_id"], user_id=s["user_id"], site=site)
        saved_sites = await self.sessions.sites(s["user_id"])
        access = build_access_plan(intent, creds, rules, session_sites=saved_sites,
                                   google_workspace=workspace, google_connect=self.google_oauth.configured)
        await self._event(s, "access_plan", access.model_dump())
        if access.blocked:
            return {"access": access.model_dump(), "route": "finalize", "final": {"kind": "blocked", "message": access.blocked}}
        if access.login == "connect_google":                      # one click on Google's own consent page, no browser takeover
            return {"access": access.model_dump(), "route": "finalize", "final": {"kind": "connect_google", "message":
                    "To read your Gmail or Drive I need you to connect your Google account once. It opens Google's own "
                    "permission page; I never see your password, and you can remove it any time under Connections."}}
        if access.login == "need_credentials":                    # nothing usable saved: ask in chat, never open a browser for the user
            return await self._ask_in_chat(s, access, purpose="connect", reason=None, known=[c.id for c in creds],
                                           extra={"intent": intent.model_dump()})
        await self._set_status(s["task_id"], "planning")
        route = "explore" if self._explore_ok(intent, access, saved_sites) else "plan"
        return {"intent": intent.model_dump(), "access": access.model_dump(), "route": route}

    def _explore_ok(self, intent: Intent, access: AccessPlan, saved_sites: set[str]) -> bool:
        """The router: the harness only for read-only work with no login, no secrets and nothing to approve (decided in code).
        A site the user is signed in to is never explored: the browser would restore that session and the harness would read
        their account pages without the safety path."""
        return (self.s.explore == "auto" and self.harness is not None and self.harness.available
                and explore_eligible(intent, access) and not set(access.allowed_domains) & saved_sites)

    # ── explore: the pi harness, boxed in; anything it cannot do falls back to the deterministic path ──
    async def n_explore(self, s: State) -> State:
        intent, access = Intent.model_validate(s["intent"]), self._access(s)
        await self._set_status(s["task_id"], "running")
        await self._event(s, "explore_start", {"sites": access.allowed_domains})
        live = await self._ensure_browser(s, headless=True)
        res = await self.harness.run(task_id=s["task_id"], goal=intent.goal, constraints="; ".join(intent.constraints),
                                     memory=memory_block(s.get("memories", [])), domains=access.allowed_domains,
                                     start_urls=start_urls(intent.sites, access.allowed_domains),
                                     cdp_url=live.cdp_url, emit=lambda t, p: self._event(s, t, p))
        current_task.set(s["task_id"])
        tel.add_usage(res.input_tokens, res.output_tokens)             # pi's own model call counts toward the task's cost
        await self._event(s, "explore_end", {"ok": res.ok, "pages": res.calls, "blocked": res.blocked,
                                             "escalate": bool(res.escalate), "error": res.error[:80],
                                             "tokens": res.input_tokens + res.output_tokens, "estimated": res.estimated})
        if res.flagged:
            tel.inc("gateway_blocks_total", layer="L2")
            await self._event(s, "gateway_block", {"layer": "L2", "source": "explore"})
        if not res.ok or res.escalate or not res.visits:
            # it hit a wall, failed, or answered without reading anything: the deterministic graph takes over from here
            await self._event(s, "explore_fallback", {"why": "escalated" if res.escalate else ("error" if res.error else "no_pages")})
            return {"route": "plan"}
        answer = redact(res.answer)
        plan = Plan(subgoals=[Subgoal(id=1, goal=intent.goal, success_criterion="the answer is supported by the pages that were read")])
        pages = "\n\n".join(f"[{v.url}]\n{v.text}" for v in res.visits)[:24_000]
        return {"plan": plan.model_dump(), "idx": 1, "evidence": [f"Read {len(res.visits)} page(s): " + ", ".join(v.url for v in res.visits[:5])],
                "claims": [answer[:2500]], "api_url": res.visits[-1].url, "api_text": pages, "explored": True,
                "suspicious": [*s.get("suspicious", []), *res.flagged][:6],
                "observed": sorted({registrable_domain(v.url) for v in res.visits if registrable_domain(v.url)}), "route": "verify"}

    # ── ask for access in chat (secure form), then sign in ourselves ──
    async def _ask_in_chat(self, s: State, access: AccessPlan, *, purpose: str, reason: str | None, known: list[str],
                           extra: State | None = None) -> State:
        ask = access.ask
        if ask is None:                                           # mid-run (expired or rejected login): build the same question here
            site = access.allowed_domains[0]
            rules = await self.svc.rules.resolve(org_id=s["org_id"], user_id=s["user_id"], site=site)
            ask = credential_ask(site, hints_for(site), rules, Intent.model_validate(s["intent"]))
            access.ask, access.login, access.connect_site = ask, "need_credentials", site
        pending = {"type": "need_credentials", "purpose": purpose, "site": ask.site, "kinds": ask.kinds,
                   "google_signin": ask.google_signin, "allow_takeover": ask.allow_takeover,
                   "reason": reason or ask.reason, "notes": access.notes[-3:], "attempt": s.get("ask_retries", 0)}
        await self._set_status(s["task_id"], "awaiting_access")
        await self._event(s, "need_credentials", {k: pending[k] for k in ("purpose", "site", "kinds", "attempt")})
        return {**(extra or {}), "access": access.model_dump(), "pending": pending, "known_creds": known, "route": "ask"}

    async def n_ask_credentials(self, s: State) -> State:
        return {"resume": interrupt(s["pending"])}

    async def n_credentials_check(self, s: State) -> State:
        v, pending, access = s.get("resume") or {}, s["pending"], self._access(s)
        site = pending["site"]
        if v.get("takeover") and pending.get("allow_takeover"):  # the explicit, opt-in "Sign in once" window
            access.login, access.connect_site = "takeover", site
            await self._event(s, "signin_once_requested", {"site": site})
            return {"access": access.model_dump(), "pending": None, "route": "connect"}
        if not v.get("added"):
            return {"route": "finalize", "pending": None, "final": {"kind": "cancelled"}}
        creds = await self.vault.list(s["user_id"])
        rules = await self.svc.rules.resolve(org_id=s["org_id"], user_id=s["user_id"], site=site)
        known = set(s.get("known_creds", []))
        fresh = [c for c in creds if c.id not in known]
        usable = usable_credentials(site, creds, rules)
        reauth = pending.get("purpose") == "reauth"
        if reauth:                                                # the old login already failed: only something new can help
            usable = [u for u in usable if u.credential_id not in known]
        hints = hints_for(site)
        ok = bool(usable) or (not reauth and bool(api_credentials(site, creds, rules)) and bool(hints.api)) or \
            (not reauth and site == "google.com" and bool(google_credentials(creds)))
        if ok and reauth and s.get("plan"):                       # mid-run: sign in with the new login, then carry on
            plan = self._plan(s)
            access.login, access.credentials, access.connect_site, access.ask = "password", usable, None, None
            self._insert_sign_in(plan, site, s["idx"])
            await self._event(s, "reauth", {"site": site, "method": "added_login"})
            await self._set_status(s["task_id"], "running")
            return {"access": access.model_dump(), "plan": plan.model_dump(), "pending": None, "route": "run", "healthy": True,
                    "attempts": 0, "just_connected": site}
        if ok:                                                    # the plan is built once, from the real login state
            return {"pending": None, "route": "access", "just_connected": site}
        tries = s.get("ask_retries", 0) + 1
        if tries > self.s.ask_credentials_max_retries:
            return {"route": "finalize", "pending": None, "final": {"kind": "blocked", "message":
                    f"I still can't find a login I can use for {site}. Nothing was saved for it, so I stopped. "
                    "Add it under Connections and ask me again."}}
        same_site = [c for c in fresh if any(registrable_domain(o) == site for o in c.origins)]
        if same_site and not usable:
            kinds = ", ".join(sorted({c.kind.replace("_", " ") for c in same_site}))
            reason = (f"I see a new {kinds} for {site}, but this step needs a website login (username and password), "
                      "which that can't provide. Add a login, or choose Sign in once.")
        elif fresh:
            kinds = ", ".join(sorted({c.kind.replace("_", " ") for c in fresh}))
            origins = ", ".join(sorted({o for c in fresh for o in c.origins}))
            reason = f"I see a new {kinds} saved for {origins}, but this task is on {site}. Please add one for {site}."
        else:
            reason = f"I don't see anything new saved for {site}. Fill in the form above, then click \"I've added it\"."
        await self._event(s, "need_credentials_retry", {"site": site, "attempt": tries})
        return {"pending": {**pending, "reason": redact(reason), "attempt": tries}, "ask_retries": tries,
                "known_creds": [c.id for c in creds], "route": "ask"}

    @staticmethod
    def _insert_sign_in(plan: Plan, site: str, idx: int, via: str | None = None) -> None:
        """Put a sign-in step in front of the step that needs it (unless that step already is one)."""
        if idx < len(plan.subgoals) and plan.subgoals[idx].is_auth:
            return
        plan.subgoals.insert(idx, sign_in_subgoal(site, max(g.id for g in plan.subgoals) + 1, via))

    # ── connect (takeover login), split so the browser opens exactly once ──
    async def n_connect_prepare(self, s: State) -> State:
        access = self._access(s)
        site = access.connect_site or access.allowed_domains[0]
        hints = hints_for(site)
        live = await self._ensure_browser(s, headless=self._in_page())
        await self._open_login_page(live, hints.auth_check_url or f"https://{site}")
        pending = {"type": "takeover", "purpose": "connect", "site": site, "live_view_url": live.live_view_url,
                   "in_page": self._shows_in_page(live),
                   "reason": f"Sign in to {site} yourself. I can't see or type anything while you do.", "notes": hints.notes}
        await self._set_status(s["task_id"], "awaiting_user")
        await self._event(s, "takeover", {k: v for k, v in pending.items()})
        return {"pending": pending}

    async def n_connect_wait(self, s: State) -> State:
        return {"resume": interrupt(s["pending"])}

    async def n_connect_finish(self, s: State) -> State:
        site = s["pending"]["site"]
        if not (s.get("resume") or {}).get("done"):
            return {"route": "finalize", "final": {"kind": "cancelled"}, "pending": None}
        live = self._live.get(s["task_id"])
        if live is None:                          # the secure window sat idle and was closed
            return {"route": "finalize", "pending": None, "final": {"kind": "failed", "reason": "The browser was closed after "
                    f"{self.s.browser_idle_seconds // 60} minutes of inactivity. Please ask again."}}
        access = self._access(s)
        signed_in = True                          # "a cookie exists" proves little (Google sets cookies for everyone): ask the site
        try:
            signed_in = await self.sessions.is_logged_in(live.cdp_url, hints_for(site))
        except Exception:
            log.warning("sign-in check failed for %s; falling back to the saved cookies", site, exc_info=True)
        try:
            saved = signed_in and await self.sessions.capture(s["user_id"], live.cdp_url, site)
        except Exception:                         # the browser could not be read (it is still open and signed in): ask again, never crash
            log.warning("could not read the browser to save the %s session", site, exc_info=True)
            tries = s.get("connect_retries", 0) + 1
            if tries > MAX_CONNECT_RETRIES:
                return {"route": "finalize", "pending": None,
                        "final": {"kind": "blocked", "message": f"I could not read the browser to save your {site} sign-in. "
                                                                "Nothing was saved. Ask me again and I will pick up where you are."}}
            pending = {**s["pending"], "reason": f"I could not read the browser just a moment ago. Your sign-in is still open: "
                                                 f"click Done again in a few seconds."}
            await self._event(s, "connect_retry", {"site": site, "attempt": tries})
            return {"route": "connect_retry", "pending": pending, "connect_retries": tries}
        if not saved:
            why = "the page still asks you to sign in" if not signed_in else "no sign-in cookies were found"
            return {"route": "finalize", "pending": None,
                    "final": {"kind": "blocked", "message": f"I did not see a sign-in to {site}: {why}. Nothing was saved."}}
        access.login, access.connect_site = "session", None
        await self._event(s, "connected", {"site": site})
        self.sessions.remember_health(s["user_id"], site, True)
        # No plan yet means we came from the access step; otherwise we are mid-run after an expired session.
        return {"access": access.model_dump(), "pending": None, "healthy": True, "just_connected": site,
                "route": "run" if s.get("plan") else "plan"}

    # ── 3. plan ──
    async def _plan_google(self, s: State, intent: Intent) -> State:
        """Gmail/Drive via the API: one read-only step, plus a separate send step (always approved) when asked to send."""
        sends = "send_message" in intent.actions
        subgoals = [Subgoal(id=1, goal=("Find what is needed and write the email (do not send it): " if sends else "") + intent.goal,
                            success_criterion="an email draft is ready" if sends else "the answer is based on the user's Gmail/Drive data")]
        if sends:
            subgoals.append(Subgoal(id=2, goal="Send the drafted email exactly as approved", irreversible=True,
                                    success_criterion="Gmail accepted the message"))
        plan = Plan(subgoals=subgoals)
        await self._event(s, "plan", {"subgoals": [sg.model_dump() for sg in plan.subgoals], "via": "google_api"})
        await self._set_status(s["task_id"], "running")
        return {"plan": plan.model_dump(), "idx": 0, "evidence": [], "attempts": 0, "route": "gate"}

    async def _google_token(self, s: State) -> str:
        """Decrypt the refresh token through the broker (origin binding, single-use agent token, audit log), trade it for
        a short-lived access token, and drop it. The refresh token is never put in state, events or a prompt."""
        cu = next((c for c in self._access(s).credentials if c.kind == "oauth"), None)
        if cu is None:
            raise OAuthError("Your Google account is not connected. Connect Google first.")
        broker = Broker(self.vault, user_id=s["user_id"], task_id=s["task_id"], org_id=s["org_id"], identity=self.identity)
        data = await broker.vault_use_api_key(cu.credential_id, "oauth2.googleapis.com")
        return await self.google_oauth.access_token(data["refresh_token"])

    async def _run_google(self, s: State) -> State:
        plan, intent = self._plan(s), Intent.model_validate(s["intent"])
        sg = plan.subgoals[s["idx"]]
        await self._event(s, "subgoal_start", {"subgoal": sg.id, "goal": sg.goal})
        try:
            token = await self._google_token(s)
        except (OAuthError, VaultError) as e:
            await self._set_status(s["task_id"], "blocked")
            return {"route": "finalize", "final": {"kind": "connect_google", "message": str(e) or "Connect Google again."}}
        if sg.irreversible:                                       # the approved send: exactly the draft the user saw
            d = s.get("draft") or {}
            if not self._guard(s).approved(sg.id) or not d:
                return {"route": "finalize", "final": {"kind": "blocked", "message": "I won't send an email without your approval."}}
            try:
                draft = make_draft(", ".join(d["to"]), d["subject"], d["body"])
                mid = await self.google_api.send_email(draft, token=token)
            except GoogleApiError as e:
                return {"route": "finalize", "final": {"kind": "failed", "reason": str(e), "options": ["retry"]}}
            await self._event(s, "step", {"subgoal": sg.id, "text": f"Sent the email to {', '.join(draft.to)}"})
            note = f"Sent to {', '.join(draft.to)} with subject \"{draft.subject}\" (Gmail message id {mid})."
            return {"route": "gate", "idx": s["idx"] + 1, "attempts": 0, "evidence": [*s["evidence"], note],
                    "api_url": "https://mail.google.com", "api_text": (s.get("api_text") or "") + "\n" + note}
        rules = await self.svc.rules.resolve(org_id=s["org_id"], user_id=s["user_id"], site="google.com")
        try:
            out = await run_google(self.llm, self.google_api, token=token, goal=sg.goal, constraints=intent.constraints or "",
                                   rules_block=rules.prompt_block(), memory=memory_block(s.get("memories", [])),
                                   may_draft=len(plan.subgoals) > s["idx"] + 1 and plan.subgoals[s["idx"] + 1].irreversible,
                                   model=self.s.model_main, emit=lambda t, p: self._event(s, t, {**p, "subgoal": sg.id}))
        except LLMError as e:
            return {"route": "finalize", "final": {"kind": "failed", "reason": f"the model could not finish: {e}", "options": ["retry"]}}
        flagged = [*s.get("suspicious", []), *out.flagged][:6]
        if out.flagged:
            tel.inc("gateway_blocks_total", layer="L2")
            await self._event(s, "gateway_block", {"layer": "L2", "subgoal": sg.id, "source": "google"})
        if out.error:
            return {"route": "finalize", "final": {"kind": "failed", "reason": out.error, "options": ["retry"]}}
        text = "\n\n".join(out.transcript)[-20000:]
        if out.draft is not None:                                 # next: the send step, behind an approval card showing this draft
            nxt = plan.subgoals[s["idx"] + 1]
            nxt.goal = f"Send this email to {', '.join(out.draft.to)}: \"{out.draft.subject}\""
            return {"route": "gate", "idx": s["idx"] + 1, "attempts": 0, "plan": plan.model_dump(), "suspicious": flagged,
                    "draft": {"to": out.draft.to, "subject": out.draft.subject, "body": out.draft.body},
                    "evidence": [*s["evidence"], out.draft.show()], "api_url": "https://mail.google.com", "api_text": text}
        return {"route": "gate", "idx": s["idx"] + 1, "attempts": 0, "suspicious": flagged,
                "evidence": [*s["evidence"], out.answer], "claims": [redact(out.answer)[:2500]],
                "api_url": "https://www.googleapis.com", "api_text": text}

    async def _plan_api(self, s: State, intent: Intent, access: AccessPlan) -> State:
        """Read-only API route: the model picks an endpoint NAME from the site's fixed list; there is no browser plan."""
        site = access.allowed_domains[0]
        offered = ApiExecutor.endpoints(site)
        def describe(name: str, spec: dict) -> str:
            ps = "; ".join(f"{k}{' (required)' if v.get('required') else ''}: {v.get('desc', '')}" for k, v in (spec.get("params") or {}).items())
            return f"- {name}{' [changes things or costs credits; needs approval]' if needs_approval(spec) else ''}: {spec['summary']}" + (f"\n    params: {ps}" if ps else "")
        menu = "\n".join(describe(name, spec) for name, spec in offered.items())
        try:
            choice = await self.llm.json(system="Pick the one API endpoint name that answers the user's request, or 'none'. "
                                                "Only names from the list are valid. For a generating endpoint, fill params using ONLY "
                                                "what the user asked for (their own words, e.g. the description to generate); leave a param "
                                                "empty if the user did not specify it. Never invent a model id.",
                                         user=f"Request: {intent.goal}\nUser's words: {s['request'][:1500]}\nEndpoints:\n{menu}",
                                         schema=ApiChoice, model=self.s.model_fast)
        except LLMError as e:
            return {"route": "finalize", "final": {"kind": "error", "message": str(e)[:200]}}
        if choice.endpoint not in offered:
            note = " ".join(hints_for(site).notes)           # the site's own limits, not the "I'll use your saved key" line
            return {"route": "finalize", "final": {"kind": "unsupported", "message":
                    f"I can't do that through the {site} API. I can: {', '.join(offered)}. {note}".strip()}}
        spec, extra = offered[choice.endpoint], {}
        if needs_approval(spec) and spec.get("kind") != "generate":      # a write we have no safe executor for: say so, never run it
            return {"route": "finalize", "final": {"kind": "unsupported", "message":
                    f"'{choice.endpoint}' on {site} would change something, and I can't do that through the API yet."}}
        free_picked = False
        if needs_approval(spec):                                   # costs credits: validated now, and the step waits for approval
            try:
                params = check_params(spec, choice.params)
            except ApiError as e:
                return {"route": "finalize", "final": {"kind": "blocked", "message": f"{e}. Tell me and I'll try again."}}
            if find_secrets(" ".join(str(v) for v in params.values())):
                return {"route": "finalize", "final": {"kind": "secret_refused"}}
            wanted, free_asked = str(choice.params.get("model") or "").strip(), bool(re.search(r"\bfree\b", s["request"], re.I))
            alternatives: list[str] = []
            if choice.endpoint in ("text", "image") and (wanted or free_asked):
                # a model was named (maybe loosely, "Nemotron") or a free one asked for: resolve it against the site's own
                # model list, in code (a read, costs nothing). The language model never produces a model id.
                broker = Broker(self.vault, user_id=s["user_id"], task_id=s["task_id"], org_id=s["org_id"], identity=self.identity)
                data = await self.api.find_models(broker=broker, access=access, site=site)
                if data is None and (free_asked or wanted):
                    return {"route": "finalize", "final": {"kind": "blocked", "message":
                            f"I couldn't read {site}'s model list to find {'a free model' if free_asked and not wanted else repr(wanted)}, "
                            "and I won't guess a model that could use your credits. Try again in a moment, or give me the exact model id."}}
                found = self.api.rank_models(data or {}, wanted or None, free_asked, "image" if choice.endpoint == "image" else "text")
                if not found:
                    what = f"a {'free ' if free_asked else ''}model matching '{wanted}'" if wanted else "a free model"
                    return {"route": "finalize", "final": {"kind": "blocked", "message":
                            f"I couldn't find {what} on {site}, and I won't use a different one without asking. "
                            "Name another model or say you're fine with a paid default."}}
                params["model"], free_picked = found[0]["id"], found[0]["free"]
                alternatives = [m["id"] for m in found[1:3]]
            shown = f"\"{str(params.get('prompt', ''))[:200]}\"" + (f" with {params['model']}" if params.get("model") else "") \
                + (f" (other matches: {', '.join(alternatives)})" if alternatives else "")
            plan = Plan(subgoals=[Subgoal(id=1, irreversible=True, success_criterion="the site returned the generated content",
                                          goal=f"Generate {choice.endpoint} on {site}: {shown}. "
                                               + ("This model is free; it should not use credits." if free_picked else "This uses credits on your account."))])
            extra = {"api_params": {k: str(v) for k, v in params.items()}}
        else:
            plan = Plan(subgoals=[Subgoal(id=1, goal=f"Ask {site} for: {spec['summary']}",
                                          success_criterion="the response contains the requested information")])
        if self.api.needs_discovery(access, site):
            # The site's way of taking a key is neither declared nor learned: the model proposes the shapes (never the host),
            # and the first step asks the user before the key is tried on the site's own API.
            host = urlparse(hints_for(site).api["base"]).hostname or site
            tries = await api_auth.propose(self.llm, site=site, host=host, endpoints=offered, model=self.s.model_fast)
            plan = Plan(subgoals=[
                Subgoal(id=1, irreversible=True, success_criterion="the site accepted the key",
                        goal=f"Find out how {host} wants your API key. I'll try it on one read-only request to {host}, in this order: "
                             + "; ".join(f"{i}. {c.describe()}" for i, c in enumerate(tries, 1)) + f". It only ever goes to {host}."),
                *[sg.model_copy(update={"id": sg.id + 1}) for sg in plan.subgoals]])
            extra = {**extra, "api_auth_candidates": [c.to_dict() for c in tries]}
        await self._event(s, "plan", {"subgoals": [sg.model_dump() for sg in plan.subgoals], "via": "api"})
        await self._set_status(s["task_id"], "running")
        return {"plan": plan.model_dump(), "idx": 0, "evidence": [], "attempts": 0, "api_endpoint": choice.endpoint, "route": "gate", **extra}

    async def n_plan(self, s: State) -> State:
        intent, access = Intent.model_validate(s["intent"]), self._access(s)
        if access.login == "api":
            return await self._plan_api(s, intent, access)
        if access.login == "google_api":
            return await self._plan_google(s, intent)
        rules = await self.svc.rules.resolve(org_id=s["org_id"], user_id=s["user_id"],
                                             site=access.allowed_domains[0] if access.allowed_domains else None)
        guidance, used = "", []
        if self.skills is not None and access.allowed_domains:      # only approved, signed, verified skills, never raw memory
            loaded = await self.skills.load_for(org_id=s["org_id"], user_id=s["user_id"], site=access.allowed_domains[0],
                                                goal=intent.goal, plan_domains=access.allowed_domains, task_id=s["task_id"],
                                                constraints=intent.constraints)
            guidance, used = "\n".join(sk.text for sk in loaded), [sk.id for sk in loaded]
        try:
            plan = await make_plan(self.llm, intent, access, rules_block=rules.prompt_block(), guidance=guidance,
                                   memory=memory_block(s.get("memories", [])), model=self.s.model_main)
        except LLMError as e:
            return {"route": "finalize", "final": {"kind": "error", "message": str(e)[:200]}}
        await self._event(s, "plan", {"subgoals": [sg.model_dump() for sg in plan.subgoals]})
        await self._set_status(s["task_id"], "running")
        return {"plan": plan.model_dump(), "idx": 0, "evidence": [], "attempts": 0, "skills_used": used, "route": "gate"}

    # ── 4. gate -> run ──
    async def n_gate(self, s: State) -> State:
        plan = self._plan(s)
        if s["idx"] >= len(plan.subgoals):
            return {"route": "verify"}
        sg = plan.subgoals[s["idx"]]
        gate = self._guard(s).gate_subgoal(sg)
        if gate.needs_approval:
            access = self._access(s)
            rules = await self.svc.rules.resolve(org_id=s["org_id"], user_id=s["user_id"],
                                                 site=access.allowed_domains[0] if access.allowed_domains else None)
            amount, cur = s.get("amount"), s.get("currency")
            limit = rules.policies.get("spend_limit", {})
            if amount is not None and cur and not rules.within_spend(amount, cur):
                await self._set_status(s["task_id"], "blocked")
                msg = f"This comes to {cur} {amount:g}, which is above the {cur} {limit.get(cur.upper(), 0):g} limit you set, so I stopped."
                return {"route": "approve_blocked", "final": {"kind": "blocked", "message": msg}}
            warn = _warning(s.get("suspicious"))
            if limit and (amount is None or not cur):
                warn = (warn + " " if warn else "") + "I couldn't read the total, so I can't check it against your spend limit."
            card = (access.cards[0] if access.cards else None)
            await self._set_status(s["task_id"], "awaiting_approval")
            if s.get("api_auth_candidates") and s["idx"] == 0:
                gate = Gate(True, "this sends your saved key to the site to find out how it is accepted")
            pending = {"type": "approval", "subgoal": sg.goal, "reason": gate.reason,
                       "summary": s["evidence"][-1] if s.get("evidence") else "", "subgoal_id": sg.id,
                       "warning": warn, "amount": amount, "currency": cur,
                       "pay_with": f"{card.label} ••••{card.last4}" if card and card.last4 else None}
            await self._event(s, "approval", pending)
            return {"route": "approve", "pending": pending}
        return {"route": "run"}

    async def n_approve(self, s: State) -> State:
        v = interrupt(s["pending"])
        if (v or {}).get("approved"):
            await self._event(s, "approved", {"subgoal_id": s["pending"]["subgoal_id"]})
            await self._set_status(s["task_id"], "running")
            return {"approved": [*s["approved"], s["pending"]["subgoal_id"]], "pending": None, "route": "run"}
        return {"route": "finalize", "pending": None, "final": {"kind": "cancelled", "message": "Okay, I stopped before doing it."}}

    async def _run_generate(self, s: State) -> State:
        """An approved generation. Defence in depth: the Guard is asked again here, and the executor refuses without approval."""
        plan, access = self._plan(s), self._access(s)
        sg, site = plan.subgoals[s["idx"]], access.allowed_domains[0]
        approved = self._guard(s).approved(sg.id)
        if not approved:
            return {"route": "finalize", "final": {"kind": "blocked", "message": "I won't spend credits without your approval."}}
        broker = Broker(self.vault, user_id=s["user_id"], task_id=s["task_id"], org_id=s["org_id"], identity=self.identity)
        await self._event(s, "subgoal_start", {"subgoal": sg.id, "goal": sg.goal})
        try:
            res = await self.api.generate(broker=broker, access=access, site=site, endpoint=s["api_endpoint"],
                                          params=s.get("api_params") or {}, user_id=s["user_id"], approved=approved)
        except ApiError as e:
            await self._set_status(s["task_id"], "blocked")
            return {"route": "finalize", "final": {"kind": "failed", "reason": str(e), "options": ["retry"]}}
        media = [{"kind": m.kind, **({"path": m.path} if m.path else {"url": m.url})} for m in res.media]
        await self._event(s, "step", {"subgoal": sg.id, "text": f"{site} {s['api_endpoint']}: {res.text[:120]}"})
        await self._event(s, "generated", {"site": site, "endpoint": s["api_endpoint"], "count": len(media)})
        gw = inspect_page_text(res.text)                           # generated text is data, not instructions
        return {"route": "gate", "idx": s["idx"] + 1, "attempts": 0, "evidence": [*s["evidence"], res.text[:400]],
                "api_url": res.url, "api_text": gw.text[:8000], "api_media": media, "api_content": (res.text if not gw.suspicious else "")[:6000],
                "suspicious": [h.excerpt for h in gw.hits][:3]}

    async def _run_discover(self, s: State) -> State:
        """The approved first step of an API task on a site whose key style is unknown: try the proposed ways, remember the one that works."""
        plan, access = self._plan(s), self._access(s)
        sg, site = plan.subgoals[s["idx"]], access.allowed_domains[0]
        broker = Broker(self.vault, user_id=s["user_id"], task_id=s["task_id"], org_id=s["org_id"], identity=self.identity)
        await self._event(s, "subgoal_start", {"subgoal": sg.id, "goal": sg.goal})
        tries = [c for c in (api_auth.try_parse(raw) for raw in s["api_auth_candidates"]) if c]
        try:
            scheme = await self.api.discover(broker=broker, access=access, site=site, candidates=tries, approved=self._guard(s).approved(sg.id))
        except ApiError as e:
            await self._set_status(s["task_id"], "blocked")
            return {"route": "finalize", "final": {"kind": "failed", "reason": str(e), "options": ["retry"]}}
        for c in access.credentials:
            if c.kind == "api_key":
                c.auth_scheme = scheme.to_dict()
        await self._event(s, "api_scheme", {"site": site, "how": scheme.describe()})
        return {"route": "gate", "idx": s["idx"] + 1, "attempts": 0, "access": access.model_dump(), "api_auth_candidates": []}

    async def _run_api(self, s: State) -> State:
        if s.get("api_auth_candidates") and s["idx"] == 0:
            return await self._run_discover(s)
        plan, access = self._plan(s), self._access(s)
        sg, site = plan.subgoals[s["idx"]], access.allowed_domains[0]
        if needs_approval(ApiExecutor.endpoints(site).get(s.get("api_endpoint", "")) or {}):
            return await self._run_generate(s)
        broker = Broker(self.vault, user_id=s["user_id"], task_id=s["task_id"], org_id=s["org_id"], identity=self.identity)
        await self._event(s, "subgoal_start", {"subgoal": sg.id, "goal": sg.goal})
        try:
            res = await self.api.call(broker=broker, access=access, site=site, endpoint=s["api_endpoint"])
        except ApiError as e:
            await self._set_status(s["task_id"], "blocked")
            return {"route": "finalize", "final": {"kind": "failed", "reason": str(e), "options": ["retry"]}}
        await self._event(s, "step", {"subgoal": sg.id, "text": f"Asked {site} for {s['api_endpoint']} (HTTP {res.status})"})
        gw = inspect_page_text(res.text)
        if gw.suspicious:
            tel.inc("gateway_blocks_total", layer="L2")
            await self._event(s, "gateway_block", {"layer": "L2", "subgoal": sg.id, "rules": sorted({h.rule for h in gw.hits}), "source": "api"})
        try:
            v = await validate(self.llm, sg, url=res.url, page_text=gw.text, screenshot=None, executor_claim="API call succeeded",
                               model=self.s.model_main)
        except LLMError as e:
            return {"route": "finalize", "final": {"kind": "failed", "reason": f"could not validate: {e}", "options": ["retry"]}}
        await self._event(s, "validated", {"subgoal": sg.id, "passed": v.passed, "confidence": v.confidence,
                                            "what_changed": v.what_changed, "evidence": v.evidence})
        if not v.passed:
            return {"route": "finalize", "final": {"kind": "failed", "reason": "the API response did not contain what was asked for", "options": ["retry"]}}
        return {"route": "gate", "idx": s["idx"] + 1, "attempts": 0, "evidence": [*s["evidence"], v.evidence or v.what_changed],
                "api_url": res.url, "api_text": gw.text[:8000], "suspicious": [h.excerpt for h in gw.hits][:3]}

    async def n_run(self, s: State) -> State:
        plan, access = self._plan(s), self._access(s)
        sg = plan.subgoals[s["idx"]]
        if access.login == "api":
            return await self._run_api(s)
        if access.login == "google_api":
            return await self._run_google(s)
        budget = self._budget.setdefault(s["task_id"], {"steps": 0, "t0": time.monotonic()})
        spent = self._spent(budget)
        if spent:
            return await self._out_of_budget(s, spent)
        live = await self._ensure_browser(s, headless=self.s.headless)

        check_saved_session = access.login == "session" and not s.get("healthy")
        check_mid_task_password = access.login == "password" and s["idx"] > 0 and not sg.is_auth
        if check_saved_session or check_mid_task_password:
            site0 = access.allowed_domains[0]
            hints = hints_for(site0)
            if check_mid_task_password:
                # Password logins can expire between subgoals too. Check the live browser directly (without the
                # short session cache) so we can put a saved sign-in step back into the plan before continuing.
                try:
                    ok = await self.sessions.is_logged_in(live.cdp_url, hints)
                except Exception:
                    log.warning("mid-task sign-in check failed", exc_info=True)
                    ok = True              # a browser/probe error is not proof that credentials were rejected
            else:
                ok = await self.sessions.check(s["user_id"], site0, live.cdp_url, hints)   # cached briefly; an error is never a cached pass
            if not ok:
                site = access.allowed_domains[0]
                await self._event(s, "session_expired", {"site": site})
                await self.sessions.revoke(s["user_id"], site, live.cdp_url)
                rules = await self.svc.rules.resolve(org_id=s["org_id"], user_id=s["user_id"], site=site)
                all_creds = await self.vault.list(s["user_id"])
                stored = usable_credentials(site, all_creds, rules)
                via_google = None
                if not stored and (sso := google_sso_login(site, Intent.model_validate(s["intent"]), all_creds, rules)):
                    stored, via_google = sso, "google"      # the user signs in to this site with Google: use the saved Google login
                    with_google(access, sso)
                if stored:        # sign in again ourselves with the saved login (placeholders, vision off)
                    if s.get("session_reauths", 0) >= 1:
                        return await self._ask_in_chat(
                            s, access, purpose="reauth", known=[c.id for c in all_creds],
                            reason=f"Your {site} session expired again after I retried the saved login. Add an updated login and I'll try once more.",
                            extra={"healthy": False})
                    access.login, access.credentials = "password", stored
                    self._insert_sign_in(plan, site, s["idx"], via_google)
                    await self._open_login_page(live, hints.auth_check_url or f"https://{site}")
                    await self._event(s, "reauth", {"site": site, "method": "stored_login"})
                    return {"access": access.model_dump(), "plan": plan.model_dump(), "route": "run", "healthy": True,
                            "attempts": 0, "session_reauths": s.get("session_reauths", 0) + 1}
                access.credentials = []                            # nothing saved: ask in chat; the browser stays hidden
                return await self._ask_in_chat(s, access, purpose="reauth", known=[c.id for c in await self.vault.list(s["user_id"])],
                                               reason=f"Your {site} sign-in has expired and I don't have a login saved for it. "
                                                      "Add one and I'll sign in myself and carry on.",
                                               extra={"healthy": False})
            s = {**s, "healthy": True}

        may_pay = sg.irreversible and self._guard(s).approved(sg.id)
        payload = await Broker(self.vault, user_id=s["user_id"], task_id=s["task_id"], org_id=s["org_id"], identity=self.identity).build(access, include_cards=may_pay)
        one_time = ""
        if code := self._take_code(s["task_id"]):                  # the user's code: this run only, typed through a placeholder
            payload.add_one_time(access.allowed_domains[0], "otp_code", code)
            one_time = ONE_TIME_NOTE
        rules = await self.svc.rules.resolve(org_id=s["org_id"], user_id=s["user_id"], site=access.allowed_domains[0])
        await self._event(s, "subgoal_start", {"subgoal": sg.id, "goal": sg.goal})
        res = await self.executor.run(session=live, subgoal=sg, access=access, guard=self._guard(s), payload=payload,
                                      rules_block=rules.prompt_block(), emit=lambda t, p: self._event(s, t, p),
                                      reflexion=s.get("reflexion", ""), progress=list(s.get("evidence") or []), one_time_note=one_time,
                                      plan_line=f"Plan: step {s['idx'] + 1} of {len(plan.subgoals)}: {sg.goal[:200]}",
                                      answers=list(s.get("answers") or []), mask=self.s.mask_tools,
                                      ask_budget=max(0, self.s.agent_ask_max - s.get("agent_asks", 0)),
                                      confirm_budget=max(0, self.s.agent_confirm_max - s.get("confirm_asks", 0)))
        budget["steps"] += res.steps
        base: State = {"healthy": True}
        if res.claim:
            base["last_claim"] = payload.redact(res.claim)[:1500]

        if res.status == "needs_approval":
            reason, extra = res.reason, {}
            if res.source == "agent":
                # The agent asked for a pause on a step the planner did not tag. Its words came from a page-driven model, so they
                # are redacted and scanned before the user reads them. This only adds a question: the Guard still decides alone
                # whether a step needs approval, and a planner-tagged step is gated before this point whatever the agent says.
                if s.get("confirm_asks", 0) >= self.s.agent_confirm_max:
                    return await self._failed(s, "the browser asked for approval too many times on this task", base)
                reason = inspect_page_text(payload.redact(res.reason or "")).text[:300]
                extra = {"confirm_asks": s.get("confirm_asks", 0) + 1}
                await self._event(s, "agent_confirm", {"subgoal": sg.id})
            await self._set_status(s["task_id"], "awaiting_approval")
            pending = {"type": "approval", "subgoal": sg.goal, "reason": reason, "summary": res.claim, "subgoal_id": sg.id,
                       "warning": _warning(s.get("suspicious"))}
            await self._event(s, "approval", pending)
            return {**base, **extra, "route": "approve", "pending": pending}
        if res.status == "needs_user":
            return await self._needs_user(s, res, access, sg, payload, base)
        if res.status == "failed":
            return await self._failed(s, res.reason, base)

        # done: check it against the page, not against the executor's claim
        url, text, shot = await observe(live.cdp_url, res.url, **observe_flags(sg))
        if shot:
            self._shots[s["task_id"]] = shot                       # for the approval card; a sign-in step has none, so a stale one is dropped
        else:
            self._shots.pop(s["task_id"], None)
        gw = await inspect_page_text_async(self.llm, payload.redact(text), enabled=self.s.classifier_enabled, threshold=self.s.classifier_threshold,
                                           model=self.s.model_main, timeout=self.s.classifier_timeout_seconds,
                                           ignore=frozenset({"credential_request"}) if sg.is_auth else frozenset())
        text, flagged = gw.text, list(s.get("suspicious", []))
        if gw.suspicious:
            flagged += [h.excerpt for h in gw.hits][:3]
            tel.inc("gateway_blocks_total", layer="L2")
            await self._event(s, "gateway_block", {"layer": "L2", "subgoal": sg.id,
                                                   "rules": sorted({h.rule for h in gw.hits})})
        base["suspicious"] = flagged
        if url.startswith(("http://", "https://")) and not url_in_plan(url, access.allowed_domains):
            tel.inc("gateway_blocks_total", layer="L5")
            await self._event(s, "gateway_block", {"layer": "L5", "subgoal": sg.id, "rules": ["off_plan_url"]})
            return await self._failed(s, "the browser ended up on a site outside this task", base)
        try:
            v = await validate(self.llm, sg, url=url, page_text=text, screenshot=shot, executor_claim=res.claim,
                               model=self.s.model_main)
        except LLMError as e:
            return await self._failed(s, f"could not validate: {e}", base)
        await self._event(s, "validated", {"subgoal": sg.id, "passed": v.passed, "confidence": v.confidence,
                                            "what_changed": v.what_changed, "evidence": v.evidence})
        site_says = await self._site_says_signed_in(s, access, live) if (not v.passed and sg.is_auth) else None     # True / False / None (could not tell)
        if site_says:
            # The validator only gets page text on a sign-in step (no screenshot), and text can miss "signed in" cues such as an
            # avatar's label (found live: Google signed in, the validator said no). The site's own signed-in page is a firmer test.
            v = v.model_copy(update={"passed": True, "evidence": "the site's own signed-in page opens without a login form",
                                     "what_changed": "signed in (confirmed by opening the site's account page)"})
            await self._event(s, "validated", {"subgoal": sg.id, "passed": True, "confidence": v.confidence,
                                                "what_changed": v.what_changed, "evidence": v.evidence, "via": "site_check"})
        if not v.passed:
            # The site's own account page asking for a login again is real evidence the login did not take; "the checker could not confirm it" is not.
            reason = ("the sign-in did not complete: the site's own account page still asks for a login" if site_says is False
                      else f"the page did not show: {sg.success_criterion}")
            return await self._failed(s, reason, base)
        seen = {"amount": v.amount, "currency": (v.currency or "").upper() or None} if v.amount is not None else {}
        dom = registrable_domain(url) if url.startswith(("http://", "https://")) else None
        base["observed"] = sorted({*s.get("observed", []), *([dom] if dom else [])})
        return {**base, **seen, "route": "gate", "idx": s["idx"] + 1, "attempts": 0, "reflexion": "",
                "evidence": [*s["evidence"], v.evidence or v.what_changed],
                "claims": [*s.get("claims", []), payload.redact(res.claim)[:2500]]}

    async def _site_says_signed_in(self, s: State, access: AccessPlan, live) -> bool | None:
        """After a sign-in step: open the site's account page in the same browser and see whether it asks for a login. Only for
        sites that declare such a page (without one the health check always says yes, which would confirm anything).
        True: signed in. False: the site still asks for a login. None: could not tell (no such page declared, or the check failed)."""
        site = access.allowed_domains[0] if access.allowed_domains else ""
        hints = hints_for(site)
        if not site or not hints.auth_check_url:
            return None
        try:
            ok = await self.sessions.is_logged_in(live.cdp_url, hints)
        except Exception:
            log.warning("signed-in check failed", exc_info=True)
            return None
        if ok:
            self.sessions.remember_health(s["user_id"], site, True)
        return ok

    async def _needs_user(self, s: State, res, access: AccessPlan, sg: Subgoal, payload, base: State) -> State:
        """The agent stopped at something it cannot do alone. Ask in chat first; a browser reaches the user only if they ask for it."""
        site = access.allowed_domains[0]
        kind = res.help_kind
        if kind == "login" and _ABOUT_A_CODE.search(res.reason or ""):
            kind = "code"                      # found live: "the OTP was wrong" filed as a login problem told the user their login was rejected
        # Page text may try to phrase a request for another site's password; the reason is scanned and cut before the user sees it.
        reason = inspect_page_text(payload.redact(res.reason or "")).text[:300]
        if (kind == "code" and sg.is_auth and s.get("totp_retries", 0) < 1
                and any(name.endswith("_2fa_code") for name in payload.placeholders)):
            # A saved TOTP is generated fresh for every Broker build. If a code expired while the browser was
            # entering it, retry once with a new code; never retry a code the user supplied or loop on rejection.
            await self._event(s, "totp_retry", {"site": site, "subgoal": sg.id})
            return {**base, "route": "run", "attempts": 0, "totp_retries": s.get("totp_retries", 0) + 1,
                    "reflexion": "The saved authenticator code was rejected; retry once with a freshly generated code."}
        if kind == "question":
            raw = payload.redact(res.reason or "")
            question = inspect_page_text(raw).text.strip()[:300]
            if not question or asks_for_secret(raw) or asks_for_secret(question):     # the raw text too: the scrubber can hide the wording
                return await self._failed(s, "the browser tried to ask for a secret in chat; secrets only go through the secure form", base)
            if s.get("agent_asks", 0) >= self.s.agent_ask_max:
                return await self._failed(s, "the browser asked too many questions on this task", base)
            pending = {"type": "ask_user", "purpose": "question", "site": site, "subgoal_id": sg.id, "question": question}
            await self._set_status(s["task_id"], "awaiting_user")
            await self._event(s, "ask_question", {"site": site, "subgoal": sg.id})
            return {**base, "route": "pause", "pending": pending, "agent_asks": s.get("agent_asks", 0) + 1}
        if kind == "unavailable":              # the thing asked for can't be had: say so now, no check, retry or replan can change that
            await self._event(s, "unavailable", {"site": site, "subgoal": sg.id})
            what = reason.strip().rstrip(".") or "what you asked for isn't available"
            return {**base, "route": "finalize", "final": {"kind": "blocked", "message":
                    f"{site} says: {what}. I stopped here instead of picking a substitute. Tell me if you'd like me to try an alternative."}}
        if kind == "code":
            if s.get("code_asks", 0) >= self.s.otp_ask_max:
                return await self._failed(s, f"{site} did not accept the verification code", base)
            pending = {"type": "ask_user", "purpose": "code", "site": site, "subgoal_id": sg.id,
                       "question": f"What's the verification code {site} just sent you? Type just the code. I use it once and don't keep it."}
            await self._set_status(s["task_id"], "awaiting_user")
            await self._event(s, "ask_code", {"site": site, "subgoal": sg.id})
            return {**base, "route": "pause", "pending": pending, "code_asks": s.get("code_asks", 0) + 1}
        if kind == "login":
            rejected = [c.id for c in await self.vault.list(s["user_id"])]
            what = (f"{site} didn't accept the saved login. Add an updated one and I'll try again." if rejected else
                    f"{site} needs you to be signed in and I don't have a login saved for it. Add one and I'll sign in myself and carry on.")
            return {**base, **await self._ask_in_chat(s, access, purpose="reauth", known=rejected, reason=what)}
        pending = {"type": "takeover", "purpose": "help", "offer_only": True, "site": site, "kind": kind,
                   "reason": reason or "I'm stuck on a step that needs you."}
        await self._set_status(s["task_id"], "awaiting_user")
        await self._event(s, "help_offered", {"site": site, "kind": kind, "subgoal": sg.id})
        return {**base, "route": "pause", "pending": pending}

    def _spent(self, budget: dict) -> str:
        """Why this task may not take another browser run, or '' if it still may."""
        if budget["steps"] >= self.s.task_max_steps:
            return f"{budget['steps']} browser steps"
        if time.monotonic() - budget["t0"] - budget.get("waited", 0.0) >= self.s.task_max_seconds:
            return f"{self.s.task_max_seconds // 60} minutes"
        return ""

    async def _out_of_budget(self, s: State, spent: str) -> State:
        await self._event(s, "budget_exhausted", {"spent": spent, "evidence": len(s.get("evidence") or [])})
        tel.inc("task_budget_exhausted_total")
        found = "; ".join(str(e) for e in (s.get("evidence") or [])[-3:]) or s.get("last_claim", "")[:600]
        reason = f"I stopped after {spent} without finishing, so I would rather not guess."
        if found:
            reason += f" What I did confirm: {found}"
        await self._set_status(s["task_id"], "blocked")
        return {"route": "finalize", "final": {"kind": "failed", "reason": reason,
                                               "options": ["retry", "take_over", "try_another_way"]}}

    async def _failed(self, s: State, reason: str, base: State) -> State:
        plan0, access0 = self._plan(s), self._access(s)
        sg0 = plan0.subgoals[s["idx"]] if s["idx"] < len(plan0.subgoals) else None
        if sg0 is not None and sg0.is_auth and access0.login == "password" and access0.credentials \
                and "outside this task" not in reason and not _NOT_A_REJECTION.search(reason) and s.get("attempts", 0) >= 1:
            # The sign-in with a SAVED login did not work. Trying the same login again cannot help (and risks a lockout):
            # say so and ask for an updated one, instead of the generic retry loop.
            site = access0.allowed_domains[0]
            await self._event(s, "login_rejected", {"site": site})
            return {**base, **await self._ask_in_chat(
                s, access0, purpose="reauth", known=[c.id for c in await self.vault.list(s["user_id"])],
                reason=f"{site} did not accept the saved login (the sign-in did not complete). "
                       "Add an updated login and I'll try again; I won't retry the old one.")}
        stalled = bool(s.get("reflexion")) and reason == s.get("reflexion")      # same failure again: retrying won't help
        if s.get("attempts", 0) < MAX_RETRIES and not stalled:
            await self._event(s, "retry", {"reason": reason, "attempt": s.get("attempts", 0) + 1})
            return {**base, "route": "run", "attempts": s.get("attempts", 0) + 1, "reflexion": reason}
        if s.get("replans", 0) < MAX_REPLANS:
            await self._event(s, "replan", {"reason": reason})
            intent, access = Intent.model_validate(s["intent"]), self._access(s)
            rules = await self.svc.rules.resolve(org_id=s["org_id"], user_id=s["user_id"], site=access.allowed_domains[0])
            done = plan0.subgoals[:s["idx"]]                                   # steps already validated: not redone by the new plan
            signed_in = access.login == "session" or any(g.is_auth for g in done)
            achieved = "; ".join(g.goal[:120] for g in done) or "nothing yet"
            try:
                plan = await make_plan(self.llm, intent, access, rules_block=rules.prompt_block(), model=self.s.model_main,
                                       signed_in=signed_in,
                                       guidance=f"An earlier plan got stuck: {reason}. Already done and checked (do NOT repeat these): {achieved}. "
                                                f"Evidence so far: {'; '.join(str(e)[:150] for e in (s.get('evidence') or [])[-4:]) or 'none'}. "
                                                "The browser is still on the page where the last step stopped. Plan only the REMAINING work, "
                                                "starting from the current page, by a different route.")
                # Progress is kept (evidence, claims); approvals are not: the new plan numbers its steps from 1, so an old
                # approval must never carry over to a different step that happens to share its number.
                return {**base, "route": "gate", "plan": plan.model_dump(), "idx": 0, "attempts": 0, "approved": [],
                        "replans": s.get("replans", 0) + 1, "reflexion": ""}
            except LLMError:
                pass
        await self._set_status(s["task_id"], "blocked")
        return {**base, "route": "finalize", "final": {"kind": "failed", "reason": reason,
                                                       "options": ["retry", "take_over", "try_another_way"]}}

    async def n_pause(self, s: State) -> State:
        v = interrupt(s["pending"]) or {}
        if s["pending"].get("purpose") == "question":          # the answer arrives as the user's chat text (secret-filtered upstream)
            answer = redact(v if isinstance(v, str) else "").strip()
            if not answer or answer.lower().rstrip(".!") in ("cancel", "stop", "never mind", "nevermind"):
                return {"route": "finalize", "pending": None, "final": {"kind": "cancelled"}}
            await self._set_status(s["task_id"], "running")
            await self._event(s, "answered", {"subgoal": s["pending"].get("subgoal_id")})     # the answer itself is not logged
            return {"route": "run", "pending": None, "attempts": 0, "answers": [*(s.get("answers") or []), answer[:300]][-3:]}
        if s["pending"].get("purpose") == "code":              # the code itself is in self._codes, never in this resume value
            if not v.get("code"):
                return {"route": "finalize", "pending": None, "final": {"kind": "cancelled"}}
            await self._set_status(s["task_id"], "running")
            return {"route": "run", "pending": None, "attempts": 0}
        if not v.get("done"):
            return {"route": "finalize", "pending": None, "final": {"kind": "cancelled"}}
        live = self._live.get(s["task_id"])
        if live and s["pending"].get("site"):                  # keep whatever the user just signed in to
            try:
                await self.sessions.capture(s["user_id"], live.cdp_url, s["pending"]["site"])
            except Exception:
                log.warning("capture after takeover failed", exc_info=True)
        await self._set_status(s["task_id"], "running")
        return {"route": "run", "pending": None, "attempts": 0}

    # ── 5. verify, remember, finalize ──
    async def n_verify(self, s: State) -> State:
        await self._set_status(s["task_id"], "verifying")
        intent, plan = Intent.model_validate(s["intent"]), self._plan(s)
        if self._access(s).login in ("api", "google_api") or s.get("explored"):
            url, text, shot = s.get("api_url", ""), s.get("api_text", ""), None          # judged against the pages that were read, not the browser
        else:
            url, text, shot = await observe(self._live[s["task_id"]].cdp_url, "", with_text=True, with_shot=True)
        gw = inspect_page_text(text)
        if gw.suspicious:
            await self._event(s, "gateway_block", {"layer": "L2", "stage": "verify",
                                                   "rules": sorted({h.rule for h in gw.hits})})
        text = gw.text
        try:
            v = await verify(self.llm, intent, plan, s["evidence"], url=url, page_text=text, screenshot=shot,
                             model=self.s.model_main, claims=s.get("claims", []))
        except LLMError as e:
            return {"final": {"kind": "result", "verified": False, "confidence": 0.0, "summary": "I finished the steps but could not verify the result.",
                              "facts": {}, "unverified": [str(e)[:120]]}}
        ok = v.confidence >= UNVERIFIED_BELOW and not v.unverified
        final = {"kind": "result", "verified": ok, "confidence": v.confidence, "summary": v.summary, "facts": v.facts,
                 "unverified": v.unverified, "screenshot_b64": base64.b64encode(shot).decode() if shot else None,
                 "url": url, "media": s.get("api_media") or [], "content": s.get("api_content") or ""}
        if s.get("just_connected") and ok:                        # fixed wording + registry only, never page text
            tip = suggestion_text(s["just_connected"])
            if tip:
                final["summary"] = f"{final['summary']}\n\n{tip}"
        return {"final": final}

    async def n_remember(self, s: State) -> State:
        """A verified run may become a learned skill. It starts untrusted: scanned, judged, signed, and approved by rule."""
        final = s.get("final") or {}
        if self.skills is None or final.get("kind") != "result" or not final.get("verified") or s.get("explored"):
            return {}                                             # an exploration has no reusable step list to learn
        try:
            access, plan, intent = self._access(s), self._plan(s), Intent.model_validate(s["intent"])
            rules = await self.svc.rules.resolve(org_id=s["org_id"], user_id=s["user_id"], site=access.allowed_domains[0])
            for sid in s.get("skills_used", []):
                await self.skills.record_use(sid, observed_domains=s.get("observed", []), verified=True, task_id=s["task_id"])
            steps = [sg.goal for sg in plan.subgoals if not sg.irreversible][:12]
            content = SkillContent(site=access.allowed_domains[0], intent=intent.goal[:200], steps=steps or [intent.goal[:200]])
            manifest_domains = sorted(set(s.get("observed", [])) & set(access.allowed_domains))
            verdict = await judge(self.llm, content, Manifest(domains=manifest_domains), model=self.s.model_fast)
            await self.skills.propose(org_id=s["org_id"], user_id=s["user_id"], content=content, plan_domains=access.allowed_domains,
                                      observed_domains=s.get("observed", []), actions=list(intent.actions),
                                      require_approval=bool(rules.policies.get("skills_require_approval")), verdict=verdict,
                                      provenance={"task_id": s["task_id"]}, constraints=intent.constraints)
        except Exception:
            log.debug("skill not saved", exc_info=True)           # learning must never break a finished task
        return {}

    async def _store_cost(self, task_id: str) -> None:
        from sqlalchemy import update
        usage = tel.pop_usage(task_id)
        try:
            async with self.svc.db.session() as s:
                await s.execute(update(Task).where(Task.id == task_id).values(cost=usage))
        except Exception:
            log.debug("cost not stored", exc_info=True)

    async def n_finalize(self, s: State) -> State:
        self._budget.pop(s["task_id"], None)
        final = s.get("final") or {"kind": "cancelled"}
        await self._cleanup(s["task_id"], capture=final.get("kind") == "result", state=s if s.get("access") else None)
        status = {"result": "done" if final.get("verified") else "unverified", "failed": "blocked",
                  "cancelled": "cancelled"}.get(final["kind"], "failed")
        await self._set_status(s["task_id"], status, final)
        tel.inc("task_finished_total", status=status)
        await self._store_cost(s["task_id"])
        await self._event(s, "final", final)
        return {"final": final, "pending": None}
