"""Chainlit UI. One chat thread; secrets never travel through it (the vault API carries them).

Flow per message: secret prefilter -> orchestrator (streams steps) -> either a pause card
(ask / approve / take over) or a final result card. Pauses are resumed from action callbacks.
"""

import base64
import logging
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import chainlit as cl
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer

from app.core.runtime import runtime
from app.progress import progress_lines
from app.memory.commands import parse_memory_command, run_memory_command
from app.security.secrets import find_secrets, redact
from app.tenancy.service import PermissionDenied

log = logging.getLogger("nami.ui")

STARTERS = [
    ("Check my OpenRouter credits", "Check how many credits I have left on OpenRouter"),
    ("Reorder my last Swiggy order", "Repeat my last Swiggy order to the same address"),
    ("See my latest Vercel deployment", "Show the status of my latest Vercel deployment"),
]


# ───────────────────────── auth ─────────────────────────

async def google_only(provider_id: str, raw_user_data: dict) -> cl.User | None:
    """Google only. Any other provider, or an unverified email, is refused."""
    if provider_id != "google" or not raw_user_data.get("email") or not raw_user_data.get("email_verified", True):
        return None
    allowed = [d.strip().lower() for d in runtime().settings.allowed_email_domains.split(",") if d.strip()]
    email = raw_user_data["email"].lower()
    if allowed and email not in allowed and email.rsplit("@", 1)[-1] not in allowed:   # entries may be domains or full addresses
        return None
    return await _sign_in(raw_user_data.get("sub") or email, email, raw_user_data.get("name"))


async def _sign_in(sub: str, email: str, name: str | None) -> cl.User:
    user = await runtime().services.tenancy.ensure_user(google_sub=sub, email=email, name=name)
    return cl.User(identifier=email, display_name=name,
                   metadata={"user_id": user.id, "org_id": user.default_org_id, "provider": "google"})


HAS_GOOGLE = bool(os.getenv("OAUTH_GOOGLE_CLIENT_ID") and os.getenv("OAUTH_GOOGLE_CLIENT_SECRET"))
DEV_AUTH = os.getenv("NAMI_DEV_AUTH") == "1"

if HAS_GOOGLE:
    @cl.oauth_callback
    async def oauth_callback(provider_id: str, token: str, raw_user_data: dict, default_user: cl.User) -> cl.User | None:
        return await google_only(provider_id, raw_user_data)

if not (HAS_GOOGLE or DEV_AUTH):
    raise RuntimeError("No sign-in configured. Set OAUTH_GOOGLE_CLIENT_ID/SECRET (production) or NAMI_DEV_AUTH=1 (local only).")

if DEV_AUTH:      # local development only: one fixed account, no Google needed      # local development only: one fixed account, no Google needed
    @cl.password_auth_callback
    async def dev_auth(username: str, password: str) -> cl.User | None:
        if username == "dev@nami.local" and password == os.getenv("NAMI_DEV_PASSWORD", "dev"):
            return await _sign_in("dev-local", username, "Dev User")
        return None


class RedactingDataLayer(SQLAlchemyDataLayer):
    """Chainlit persists whatever the user typed (message text, and a thread name taken from the first message).
    Anything secret-shaped is redacted here, at the one place everything is written, so no code path can forget."""

    @staticmethod
    def _scrub(d: dict, keys=("name", "input", "output")) -> dict:
        return {k: (redact(v) if k in keys and isinstance(v, str) else v) for k, v in d.items()}

    async def create_step(self, step_dict):
        return await super().create_step(self._scrub(step_dict))

    async def update_thread(self, thread_id, name=None, user_id=None, metadata=None, tags=None):
        return await super().update_thread(thread_id, redact(name) if isinstance(name, str) else name, user_id, metadata, tags)


@cl.data_layer
def data_layer():
    return RedactingDataLayer(conninfo="sqlite+aiosqlite:///data/chainlit.db")


# ───────────────────────── session helpers ─────────────────────────

def _ids() -> tuple[str, str]:
    meta = cl.user_session.get("user").metadata
    return meta["user_id"], meta["org_id"]


async def _history(thread_id: str) -> str:
    try:
        ctx = await runtime().services.stm.context(thread_id)
    except LookupError:
        return ""
    return "\n".join(f"{t.role}: {t.content}" for t in ctx.recent[-6:])


async def _remember_turn(role: str, text: str) -> None:
    user_id, org_id = _ids()
    svc = runtime().services
    tid = cl.context.session.thread_id
    ch = await svc.stm.ensure_channel(org_id=org_id, kind="web", external_id=user_id, name="Nami web")
    await svc.stm.open_thread(channel_id=ch.id, org_id=org_id, user_id=user_id, thread_id=tid)
    await svc.stm.append(tid, role, text, author_user_id=user_id if role == "user" else None)


class Progress:
    """One collapsible step that shows what the agent is doing, in plain language."""

    def __init__(self):
        self.step = cl.Step(name="What I'm doing", type="run")
        self.lines: list[str] = []
        self.started = False

    async def add(self, line: str) -> None:
        if not self.started:
            await self.step.send()
            self.started = True
        self.lines.append(line)
        self.step.output = "\n".join(self.lines)
        await self.step.update()

    async def emit(self, type_: str, p: dict) -> None:
        for line in progress_lines(type_, p):
            await self.add(line)


# ───────────────────────── chat lifecycle ─────────────────────────

@cl.set_starters
async def set_starters():
    return [cl.Starter(label=label, message=msg) for label, msg in STARTERS]


@cl.on_chat_start
async def on_chat_start():
    user = cl.user_session.get("user")
    await cl.ElementSidebar.set_title("Connections")
    await cl.ElementSidebar.set_elements([cl.CustomElement(name="Connections", props={})])
    await cl.Message(
        content=f"Hi {user.display_name or ''}. Tell me what you want done on a website and I'll do it in a background "
                "browser. Passwords and keys go in the secure form, never in this chat.",
        actions=[cl.Action(name="add_credential", payload={}, label="Add a login or API key")],
    ).send()


    user_id, _ = _ids()
    for task_id, request in await runtime().orchestrator.interrupted(user_id):
        await cl.Message(content=f"You have an unfinished task: **{request[:120]}**",
                         actions=[cl.Action(name="resume_task", payload={"task_id": task_id}, label="Resume"),
                                  cl.Action(name="discard_task", payload={"task_id": task_id}, label="Discard")]).send()


@cl.action_callback("resume_task")
async def resume_task(action: cl.Action):
    await action.remove()
    await _handle(await runtime().orchestrator.resume_interrupted(action.payload["task_id"], _progress().emit, user_id=_ids()[0]))


@cl.action_callback("discard_task")
async def discard_task(action: cl.Action):
    await action.remove()
    try:
        await runtime().orchestrator.cancel(action.payload["task_id"], user_id=_ids()[0])
        await cl.Message(content="Discarded.").send()
    except PermissionDenied:
        await cl.Message(content="I can't find that task.").send()


@cl.action_callback("add_credential")
async def add_credential(action: cl.Action):
    await cl.Message(content="Secrets go straight to the encrypted vault, not through the chat or the model.",
                     elements=[cl.CustomElement(name="SecureInput", props={}, display="inline")]).send()


@cl.on_chat_resume
async def on_chat_resume(thread):
    await cl.Message(content="Welcome back. If a task was waiting for you, say so and I'll pick it up.").send()


@cl.on_message
async def on_message(msg: cl.Message):
    text = msg.content or ""
    pending = cl.user_session.get("pending")
    if pending and pending["type"] == "ask_user" and pending.get("purpose") == "code":
        # The one place a bare code is accepted from the chat: only while a code is being waited for, and only if the whole
        # message is the code. Anything else (a phrased "otp: 123456 and my password is ...") falls through to the secret filter.
        code = _extract_code(text)
        if code:
            if await runtime().orchestrator.offer_code(pending["task_id"], _ids()[0], code):
                msg.content = "[code provided]"
                await msg.update()                            # the code never stays in the saved thread
                await _resume({"code": True})
            else:
                await cl.Message(content="I wasn't waiting for a code any more.").send()
            return
    if pending and pending["type"] == "need_credentials" and text.strip().lower().rstrip(".!") in ("done", "added", "saved", "added it", "i added it"):
        await _resume({"added": True})
        return
    if find_secrets(text):
        msg.content = redact(text)
        await msg.update()                                    # scrub it from the saved thread too
        await cl.Message(content="That looks like a secret, so I didn't send it anywhere. Use **Add a login or API key** "
                                 "and I'll keep it encrypted, tied to the site you choose.",
                         actions=[cl.Action(name="add_credential", payload={}, label="Add a login or API key")]).send()
        return

    if pending and pending["type"] == "ask_user" and pending.get("purpose") != "code":   # the answer to my clarifying question
        cl.user_session.set("pending", None)
        await _handle(await runtime().orchestrator.resume(pending["task_id"], text, _progress().emit, user_id=_ids()[0]))
        return
    if cl.user_session.get("task_id") and pending:
        if text.strip().lower() in ("cancel", "stop", "never mind", "nevermind"):
            await on_stop()
            await cl.Message(content="Okay, stopped. I closed the browser and kept nothing half-done.").send()
            return
        if pending.get("purpose") == "code":
            await cl.Message(content="Just the code, please (or say 'cancel').").send()
            return
        await cl.Message(content="I'm waiting on you for the step above. Use its buttons, or say 'cancel'.").send()
        return

    user_id, org_id = _ids()
    await _remember_turn("user", text)
    cmd = parse_memory_command(text)
    if cmd:                                                   # "remember that …": the user's own words, never the agent's
        reply = await run_memory_command(runtime().services.ltm, cmd, org_id=org_id, user_id=user_id,
                                         thread_id=cl.context.session.thread_id)
        await _remember_turn("assistant", reply)
        await cl.Message(content=reply).send()
        return
    cl.user_session.set("request", text)
    progress = _progress()
    out = await runtime().orchestrator.start(user_id=user_id, org_id=org_id, thread_id=cl.context.session.thread_id,
                                             text=text, history=await _history(cl.context.session.thread_id),
                                             emit=progress.emit)
    await _handle(out)


_CODE = re.compile(r"(?:(?:the\s+)?(?:otp|code|verification code|pin)\s*(?:is|:)?\s*)?([0-9A-Za-z]{4,8})", re.I)


def _extract_code(text: str) -> str | None:
    """A verification code: 4-8 letters/digits with at least one digit, optionally introduced by 'code is'. Nothing else."""
    m = _CODE.fullmatch((text or "").strip().rstrip(".!"))
    return m.group(1) if m and any(c.isdigit() for c in m.group(1)) else None


def _progress() -> Progress:
    p = Progress()
    cl.user_session.set("progress", p)
    return p


@cl.on_stop
async def on_stop():
    tid = cl.user_session.get("task_id")
    if tid:
        await runtime().orchestrator.cancel(tid, user_id=_ids()[0])
        cl.user_session.set("pending", None)
        cl.user_session.set("task_id", None)


# ───────────────────────── outcomes ─────────────────────────

async def _handle(out) -> None:
    cl.user_session.set("task_id", out.task_id)
    if out.pending:
        cl.user_session.set("pending", {**out.pending, "task_id": out.task_id})
        await _show_pending(out.pending)
        return
    cl.user_session.set("pending", None)
    cl.user_session.set("task_id", None)
    await _show_final(out.final or {"kind": "cancelled"})


async def _show_pending(p: dict) -> None:
    kind = p["type"]
    if kind == "ask_user":
        acts = [cl.Action(name="code_cancel", payload={}, label="Cancel")] if p.get("purpose") == "code" else []
        await cl.Message(content=p["question"], actions=acts).send()
    elif kind == "need_credentials":
        note = "\n".join(p.get("notes", []))
        els = [cl.CustomElement(name="SecureInput", display="inline", props={      # labels only: no secret ever goes in props
            "site": p["site"], "kinds": p.get("kinds", []), "lock_site": True, "purpose": p.get("purpose", "connect")})] if p.get("kinds") else []
        acts = [cl.Action(name="credentials_added", payload={}, label="I've added it")] if els else []
        if p.get("allow_takeover"):
            acts.append(cl.Action(name="signin_once", payload={}, label="Sign in once instead"))
        acts.append(cl.Action(name="credentials_cancel", payload={}, label="Cancel"))
        await cl.Message(content=f"{p['reason']}\n\n{note}".strip() + "\n\nYour password goes straight to the encrypted vault; "
                         "it never appears in this chat or reaches me.", elements=els, actions=acts).send()
    elif kind == "takeover" and p.get("offer_only"):
        await cl.Message(content=f"I'm stuck on a step I can't do for you: {p['reason']}\n\nIf you want, I can open a one-time secure "
                                 "window so you can do just that step. I won't open anything unless you ask.",
                         actions=[cl.Action(name="signin_once_open", payload={}, label="Open secure window"),
                                  cl.Action(name="takeover_cancel", payload={}, label="Cancel")]).send()
    elif kind == "approval":
        warn = f"\n\n⚠️ {p['warning']}" if p.get("warning") else ""
        money = f"\n\n**Total: {p['currency']} {p['amount']:g}**" if p.get("amount") is not None and p.get("currency") else ""
        money += f"  \nPay with: {p['pay_with']}" if p.get("pay_with") else ""
        body = f"**Before I do this:** {p['subgoal']}\n\n{p.get('summary') or ''}\n\n_{p['reason'].capitalize()}._{money}{warn}"
        shot = await runtime().orchestrator.approval_image(cl.user_session.get("task_id"), _ids()[0]) if cl.user_session.get("task_id") else None
        pic = [cl.Image(name="The page you are approving", content=shot, display="inline")] if shot else []     # what Nami is looking at
        await cl.Message(content=body, elements=[*pic, cl.CustomElement(name="ApprovalCard", display="inline", props={
            "title": p["subgoal"], "summary": p.get("summary", ""), "reason": p["reason"],
            "warning": p.get("warning", ""), "total": f"{p['currency']} {p['amount']:g}" if p.get("amount") is not None and p.get("currency") else "",
            "pay_with": p.get("pay_with") or ""})],
            actions=[cl.Action(name="approve", payload={}, label="Approve"),
                     cl.Action(name="deny", payload={}, label="Cancel")]).send()
    elif kind == "takeover":
        elements = []
        ticket = None
        if p.get("in_page") and cl.user_session.get("task_id"):
            ticket = runtime().orchestrator.live_ticket(cl.user_session.get("task_id"), _ids()[0])
        if p.get("live_view_url"):
            elements = [cl.CustomElement(name="LiveBrowserView", display="inline",
                                         props={"url": p["live_view_url"], "interactive": True})]
        elif ticket:                                          # streamed into this page; nothing opens on the server
            elements = [cl.CustomElement(name="LiveBrowserView", display="inline",
                                         props={"relay": f"/chainlit/live/ws/{ticket}", "interactive": True})]
        note = "\n".join(p.get("notes", []))
        where = "" if elements else "\n\n_A browser window is open on the machine running Nami._"
        await cl.Message(content=f"**You're in control.** {p['reason']}\n\n{note}{where}\n\nWhen you're done, hand it back.",
                         elements=elements,
                         actions=[cl.Action(name="handback", payload={}, label="Done, hand back"),
                                  cl.Action(name="takeover_cancel", payload={}, label="Cancel")]).send()


def _is_generated_file(path: str) -> bool:
    """Only files Nami itself saved for generations are ever attached to the chat."""
    try:
        root = Path(runtime().orchestrator.api.out_dir).resolve()
        return Path(path).resolve().is_relative_to(root) and Path(path).is_file()
    except Exception:
        return False


async def _show_final(f: dict) -> None:
    k = f["kind"]
    if k == "result":
        els = [cl.CustomElement(name="ResultCard", display="inline", props={
            "verified": bool(f["verified"]), "confidence": float(f.get("confidence", 0)), "summary": redact(f["summary"]),
            "facts": [[redact(str(a)), redact(str(b))] for a, b in (f.get("facts") or {}).items()],
            "unverified": [redact(str(u)) for u in f.get("unverified", [])], "url": redact(f.get("url", ""))})]
        if f.get("screenshot_b64"):
            els.append(cl.Image(name="Final page", content=base64.b64decode(f["screenshot_b64"]), display="inline"))
        text = redact(f["summary"])
        if f.get("content"):                                               # the generated text itself is the answer
            text += "\n\n---\n\n" + redact(str(f["content"]))
        for i, m in enumerate(f.get("media") or [], 1):                     # what a generation produced
            if m.get("path") and _is_generated_file(m["path"]):
                maker = cl.Audio if m["kind"] == "audio" else cl.Image
                els.append(maker(name=f"{m['kind'].capitalize()} {i}", path=m["path"], display="inline"))
            elif m.get("url", "").startswith("https://"):                    # a link from the API: shown, never fetched by Nami
                host = urlparse(m["url"]).hostname or ""
                text += f"\n\n[{m['kind'].capitalize()} {i}]({m['url']}) (opens {host})"
        await cl.Message(content=text, elements=els).send()
        await _remember_turn("assistant", f["summary"])
    elif k == "failed":
        await cl.Message(content=f"I couldn't finish this: {f['reason']}\n\nWhat would you like to do?",
                         actions=[cl.Action(name="retry_task", payload={}, label="Try again"),
                                  cl.Action(name="manual_takeover", payload={}, label="I'll do it myself")]).send()
    elif k == "connect_google":
        await cl.Message(content=f"{f.get('message', '')}\n\n**[Connect Google](/chainlit/connect/google/start)** "
                                 "(opens Google's permission page in a new tab). When it says *Google connected*, come back here.",
                         actions=[cl.Action(name="retry_task", payload={}, label="I've connected it, try again")]).send()
    elif k == "secret_refused":
        await cl.Message(content="That contains a secret. Please use the secure form instead.").send()
    elif k in ("blocked", "unsupported", "error"):
        await cl.Message(content=f.get("message", "I can't do that.")).send()
    else:
        await cl.Message(content=f.get("message") or "Okay, stopped.").send()


async def _resume(value) -> None:
    pending = cl.user_session.get("pending")
    if not pending:
        return
    cl.user_session.set("pending", None)
    await _handle(await runtime().orchestrator.resume(pending["task_id"], value, _progress().emit, user_id=_ids()[0]))


@cl.action_callback("approve")
async def approve(action: cl.Action):
    await action.remove()
    await _resume({"approved": True})


@cl.action_callback("deny")
async def deny(action: cl.Action):
    await action.remove()
    await _resume({"approved": False})


@cl.action_callback("handback")
async def handback(action: cl.Action):
    await action.remove()
    await _end_live_view()
    await _resume({"done": True})


@cl.action_callback("credentials_added")
async def credentials_added(action: cl.Action):
    await action.remove()
    await _resume({"added": True})


@cl.action_callback("credentials_cancel")
async def credentials_cancel(action: cl.Action):
    await action.remove()
    await _resume({"added": False})


@cl.action_callback("signin_once")
async def signin_once(action: cl.Action):
    await action.remove()
    await _resume({"takeover": True})


@cl.action_callback("code_cancel")
async def code_cancel(action: cl.Action):
    await action.remove()
    await _resume({"code": False})


@cl.action_callback("signin_once_open")
async def signin_once_open(action: cl.Action):
    """The only way a browser view reaches the screen from a pause: the user clicked for it."""
    await action.remove()
    pending = cl.user_session.get("pending")
    info = await runtime().orchestrator.surface(pending["task_id"], _ids()[0]) if pending else None
    if not info:
        await cl.Message(content="I can't open that window any more.").send()
        return
    await _show_pending({**pending, **info, "offer_only": False,
                         "reason": f"Do the step on {pending['site']} yourself. I can't see or type anything while you do."})


@cl.action_callback("takeover_cancel")
async def takeover_cancel(action: cl.Action):
    await action.remove()
    await _end_live_view()
    await _resume({"done": False})


async def _end_live_view() -> None:
    """Handing back ends the user's in-page view at once, even if the agent keeps using that browser."""
    tid = cl.user_session.get("task_id")
    if tid:
        await runtime().orchestrator.live_relay.revoke_task(tid)


@cl.action_callback("retry_task")
async def retry_task(action: cl.Action):
    await action.remove()
    text = cl.user_session.get("request")
    if text:
        user_id, org_id = _ids()
        out = await runtime().orchestrator.start(user_id=user_id, org_id=org_id, thread_id=cl.context.session.thread_id,
                                                 text=text, history="", emit=_progress().emit)
        await _handle(out)


@cl.action_callback("manual_takeover")
async def manual_takeover(action: cl.Action):
    await action.remove()
    await cl.Message(content="Okay. I stopped and left the browser alone. Tell me when you want me to try again.").send()
