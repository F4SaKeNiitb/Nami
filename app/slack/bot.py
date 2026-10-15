"""Nami in Slack direct messages. The same orchestrator the web chat uses; only the surface differs.

What is different from the web chat, and why:
  * Slack stores every message, so nothing secret is collected here. Passwords and API keys go to the web app's vault; a
    verification code is entered in a modal (its value is never a message). If someone pastes a secret anyway it is refused,
    never forwarded, and they are told Slack still has a copy.
  * Who is asking is a Nami user the Slack account was explicitly linked to (see links.py); every call then carries that user id,
    so the orchestrator's own owner checks apply to a button click exactly as they do on the web.
  * Direct messages only: results can contain orders, balances or emails, and a channel would show them to everyone in it.
"""

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from app.db.base import new_id
from app.memory.commands import parse_memory_command, run_memory_command
from app.progress import progress_lines
from app.security.secrets import find_secrets
from app.slack import blocks
from app.slack.blocks import message

log = logging.getLogger("nami.slack")

# Slack turns any URL in a message into a preview card (site title, description, homepage image). The agent's step lines are full of
# URLs it visits, so every progress edit grew a card. Nothing the bot says wants one: links are shown as links.
NO_PREVIEW = {"unfurl_links": False, "unfurl_media": False}

_CANCEL = ("cancel", "stop", "never mind", "nevermind")
_DONE = ("done", "added", "saved", "added it", "i added it")
_CODE_OK = re.compile(r"[0-9A-Za-z]{4,8}")

ACTIONS = {                               # action_id -> (what the card becomes, value handed to the orchestrator)
    "nami_approve": ("✓ Approved", {"approved": True}),
    "nami_deny": ("Cancelled", {"approved": False}),
    "nami_added": ("✓ Thanks, checking it now", {"added": True}),
    "nami_creds_cancel": ("Cancelled", {"added": False}),
    "nami_code_cancel": ("Cancelled", {"code": False}),
    "nami_takeover_cancel": ("Cancelled", {"done": False}),
    "nami_takeover_done": ("✓ Thanks, continuing", {"done": True}),
}


@dataclass
class Conv:
    user_id: str
    org_id: str
    thread_id: str
    task_id: str | None = None
    pending: dict | None = None
    card_ts: str | None = None
    request: str = ""
    busy: bool = False
    run_task: "asyncio.Future | None" = None       # the run in flight, so Stop can cancel it
    run_id: str = ""                               # tells this run's Stop button from an old one
    stopping: bool = False                         # set only by the user's Stop: a shutdown cancels the same task but is not a user stop


class SlackProgress:
    """One message that is edited as the agent works. Edits are spaced out so Slack's rate limit is never the thing that fails."""

    def __init__(self, client, channel: str, *, min_gap: float = 1.5, clock=time.monotonic, stop_value: str | None = None):
        self.client, self.channel, self.min_gap, self.clock = client, channel, min_gap, clock
        self.stop_value = stop_value                 # the run id its Stop button carries
        self.lines: list[str] = []
        self.ts: str | None = None
        self._last = float("-inf")
        self._lock = asyncio.Lock()

    async def emit(self, type_: str, p: dict) -> None:
        new = progress_lines(type_, p)
        if new:
            self.lines += new
            await self._push(force=self.ts is None)

    async def flush(self) -> None:
        if self.lines:
            await self._push(force=True, working=False)

    async def _push(self, *, force: bool = False, working: bool = True) -> None:
        async with self._lock:
            now = self.clock()
            if not force and now - self._last < self.min_gap:
                return
            self._last = now
            msg = blocks.progress_message(self.lines, working, stop_value=self.stop_value)
            try:
                if self.ts is None:
                    self.ts = (await self.client.chat_postMessage(channel=self.channel, **msg, **NO_PREVIEW))["ts"]
                else:
                    await self.client.chat_update(channel=self.channel, ts=self.ts, **msg, **NO_PREVIEW)
            except Exception as e:                        # a failed progress edit must never fail the task
                log.warning("progress update failed: %s", type(e).__name__)


class SlackBot:
    def __init__(self, *, client, orchestrator, links, stm, ltm, web_url: str, prober=None):
        self.client, self.orch, self.links, self.stm, self.ltm = client, orchestrator, links, stm, ltm
        self.prober = prober                              # looks at a site's real sign-in page; None = the form falls back to the usual fields
        self.web_url = web_url.rstrip("/")
        self.convs: dict[tuple[str, str], Conv] = {}
        self.extra: dict[tuple[str, str], list[Conv]] = {}   # more requests running at the same time as the main one, each with its own task
        self._seen: dict[str, None] = {}                  # recently handled message ids, so a redelivery does nothing
        self._tasks: set[asyncio.Task] = set()

    def spawn(self, coro) -> None:
        """Run long work off the event handler (Slack wants an answer in seconds; a task takes minutes)."""
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def stop(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _conv(self, team: str, channel: str, who) -> tuple[Conv, bool]:
        """This DM's conversation. After a restart nothing is in memory, but the task's pause is in the saved checkpoint and its
        thread id is derived from (workspace, channel), so a card that was waiting is found again. Returns (conv, found_again)."""
        conv = self.convs.get((team, channel))
        if conv is None or conv.user_id != who.user_id:
            conv = self.convs[(team, channel)] = Conv(who.user_id, who.org_id, hashlib.sha256(f"{team}:{channel}".encode()).hexdigest()[:32])
        if conv.task_id or conv.pending or conv.busy:
            return conv, False
        found = await self.orch.paused_in_thread(who.user_id, conv.thread_id)
        if not found:
            return conv, False
        conv.task_id, conv.pending = found[0], {**found[1], "task_id": found[0]}
        return conv, True

    def _owning(self, team: str, channel: str, main: Conv, ident: str) -> Conv:
        """The job a button or modal belongs to: its task id or its run id is on the button. Falls back to the main conversation."""
        for c in [main, *self.extra.get((team, channel), [])]:
            if ident and ident in (c.task_id, c.run_id):
                return c
        return main

    def _running(self, team: str, channel: str, main: Conv) -> list[Conv]:
        return [c for c in [main, *self.extra.get((team, channel), [])] if c.busy]

    def _drop_finished(self, team: str, channel: str) -> None:
        keep = [c for c in self.extra.get((team, channel), []) if c.busy or c.pending]
        if keep:
            self.extra[(team, channel)] = keep
        else:
            self.extra.pop((team, channel), None)

    def card(self, conv: Conv, pending: dict, task_id: str) -> dict:
        """The Slack message for a pause. A login/key request gets its own one-time link to a page asking for just what is needed."""
        add_url = None
        if pending["type"] == "need_credentials" and set(pending.get("kinds", [])) & {"password", "api_key"}:
            token = self.links.begin_add(conv.user_id, pending["site"], pending["kinds"])
            if self.prober is not None and "password" in pending["kinds"]:
                self.links.watch_shape(token, self.prober.shape(pending["site"]))       # starts now; ready by the time the link is opened
            add_url = f"{self.web_url}/chainlit/slack/add?token={token}"
        window_url = None
        if pending["type"] == "takeover" and pending.get("offer_only"):
            window_url = f"{self.web_url}/chainlit/slack/solve?token={self.links.begin_solve(conv.user_id, task_id, pending.get('site', ''))}"
        return blocks.pending_message(pending, task_id, f"{self.web_url}/chainlit", add_url, window_url)

    # ───────────────────────── messages ─────────────────────────

    async def _slack_email(self, user: str) -> str:
        try:
            info = await self.client.users_info(user=user)
        except Exception as e:                          # a missing scope or a Slack outage: say so below, don't crash the handler
            log.warning("slack users.info failed: %s", type(e).__name__)
            return ""
        return str(((info.get("user") or {}).get("profile") or {}).get("email") or "")

    async def _link_from_profile(self, team: str, channel: str, user: str):
        """First message from an unknown Slack account: match its profile email to a Nami user. Nothing for the person to confirm."""
        email = await self._slack_email(user)
        if not email:
            await self._post(channel, message("I couldn't read the email on your Slack profile, so I don't know who you are yet. "
                                              "Ask the workspace admin to give the Nami app the `users:read.email` permission."))
            return None
        who = await self.links.link_by_email(team, user, email)
        if not who:
            await self._post(channel, message(f"I don't have a Nami account for {email}. Sign in to the Nami web app once with that Google "
                                              f"account ({self.web_url}), then message me again."))
        return who

    async def on_direct_message(self, *, team: str, channel: str, user: str, text: str, ts: str) -> None:
        if f"{channel}:{ts}" in self._seen:
            return
        self._seen[f"{channel}:{ts}"] = None
        while len(self._seen) > 500:
            self._seen.pop(next(iter(self._seen)))
        text = (text or "").strip()
        if not text:
            return
        low = text.lower().rstrip(".!?")
        who = await self.links.resolve(team, user)
        if low == "unlink":
            await self._post(channel, message("Unlinked. I won't act for this Slack account any more." if who and await self.links.unlink(team, user)
                                              else "This Slack account isn't linked."))
            self.convs.pop((team, channel), None)
            self.extra.pop((team, channel), None)
            return
        if not who:
            who = await self._link_from_profile(team, channel, user)
            if not who:
                return
        if low in ("help", "hi", "hello"):
            await self._post(channel, message(blocks.HELP))
            return
        conv, again = await self._conv(team, channel, who)
        if conv.busy:
            running = self._running(team, channel, conv)
            if low in _CANCEL:                                       # "cancel" works mid-run too: it stops everything that is running
                if [await self.stop_run(c) for c in running].count(True):
                    return
            elif find_secrets(text):
                await self._post(channel, message("That looks like a secret, so I didn't use it. To give me access safely, add it in the Nami web app: "
                                                  f"{self.web_url}/chainlit"))
                return
            else:                                                    # a second request while one is running: it runs alongside, as its own job
                await self._start_alongside(conv, team, channel, text, ts)
                return
        p = conv.pending
        answers_it = low in _CANCEL or (p and ((p["type"] == "ask_user" and p.get("purpose") != "code")
                                               or (p["type"] == "need_credentials" and low in _DONE)))
        if again and p and not answers_it:                 # I was restarted while waiting on you: show the card again, run nothing
            await self._post(channel, message("I was restarted while waiting on you. Here is where we left off:"))
            conv.card_ts = await self._post(channel, self.card(conv, p, conv.task_id))
            return
        if p and low in _CANCEL:
            await self._cancel(conv, channel)
            return
        if p and p["type"] == "ask_user" and p.get("purpose") == "code":
            await self._post(channel, message("Please press *Enter code* on the card above. Typing it here would leave it in this "
                                              "conversation. (Or say *cancel*.)"))
            return
        if p and p["type"] == "need_credentials" and low in _DONE:
            await self._resume(conv, team, channel, {"added": True})
            return
        if find_secrets(text):
            await self._post(channel, message(
                "That looks like a secret, so I didn't use it or send it anywhere. Slack has already stored your message though: delete it, "
                f"and if it was real, change it. To give me access safely, add it in the Nami web app: {self.web_url}/chainlit"))
            return
        if p and p["type"] == "ask_user":                 # the answer to my clarifying question
            await self._resume(conv, team, channel, text)
            return
        if p:
            await self._post(channel, message("I'm waiting on you for the step above. Use its buttons, or say *cancel*."))
            return
        await self._start(conv, team, channel, text)

    async def on_mention(self, *, channel: str, ts: str) -> None:
        await self._post(channel, message("Message me directly instead: my answers can include personal details, and a channel would show them to everyone in it."),
                         thread_ts=ts)

    async def _start(self, conv: Conv, team: str, channel: str, text: str) -> None:
        conv.request = text
        await self._remember(conv, team, channel, "user", text)
        cmd = parse_memory_command(text)
        if cmd:                                            # "remember that ...": the user's own words, never the agent's
            reply = await run_memory_command(self.ltm, cmd, org_id=conv.org_id, user_id=conv.user_id, thread_id=conv.thread_id)
            await self._remember(conv, team, channel, "assistant", reply)
            await self._post(channel, message(reply))
            return
        history = await self._history(conv.thread_id)
        task_id = conv.task_id = new_id()                     # chosen here, so Stop knows which task to cancel while it is still running
        await self._run(conv, team, channel, lambda emit: self.orch.start(
            user_id=conv.user_id, org_id=conv.org_id, thread_id=conv.thread_id, text=text, history=history, emit=emit, task_id=task_id))

    async def _start_alongside(self, main: Conv, team: str, channel: str, text: str, ts: str) -> None:
        """Run another request at the same time as the ones already going. Its own thread and task, so its pauses, cards and Stop
        button are separate; the web app and Slack share the same per-user cap (the orchestrator says no past it)."""
        jobs = self.extra.setdefault((team, channel), [])
        job = Conv(main.user_id, main.org_id, hashlib.sha256(f"{team}:{channel}:{ts}".encode()).hexdigest()[:32])
        jobs.append(job)
        await self._post(channel, message("On it. I'm running this one alongside the other. Each has its own progress message and *Stop* button."))
        try:
            await self._start(job, team, channel, text)
        finally:
            self._drop_finished(team, channel)

    async def _resume(self, conv: Conv, team: str, channel: str, value) -> None:
        task_id = conv.task_id
        conv.pending = None
        try:
            await self._run(conv, team, channel, lambda emit: self.orch.resume(task_id, value, emit, user_id=conv.user_id))
        finally:
            self._drop_finished(team, channel)

    async def _cancel(self, conv: Conv, channel: str) -> None:
        if conv.task_id:
            try:
                await self.orch.cancel(conv.task_id, user_id=conv.user_id)
            except Exception:
                log.warning("cancel failed", exc_info=False)
        conv.pending, conv.task_id = None, None
        await self._post(channel, message("Okay, stopped. I closed the browser and kept nothing half-done."))

    async def stop_run(self, conv: Conv) -> bool:
        """Stop the run that is going now. True if there was one. `_run` does the tidying and the messages once the run has actually ended."""
        if not conv.busy or conv.run_task is None or conv.run_task.done():
            return False
        conv.stopping = True
        conv.run_task.cancel()
        return True

    async def _run(self, conv: Conv, team: str, channel: str, call) -> None:
        conv.busy, conv.stopping, conv.run_id = True, False, secrets.token_urlsafe(8)
        prog = SlackProgress(self.client, channel, stop_value=conv.run_id)
        conv.run_task = asyncio.ensure_future(call(prog.emit))
        try:
            out = await conv.run_task
        except asyncio.CancelledError:
            if not conv.stopping:                              # not the user: the process is shutting down; take the run with us
                conv.run_task.cancel()
                raise
            await self._stopped(conv, channel, prog)
            return
        except Exception:
            log.exception("slack task failed")
            conv.pending, conv.task_id = None, None
            await prog.flush()
            await self._post(channel, message("Something went wrong on my side. Please try again."))
            return
        finally:
            conv.busy, conv.run_task = False, None
        await prog.flush()
        await self._outcome(conv, team, channel, out)

    async def _stopped(self, conv: Conv, channel: str, prog: SlackProgress) -> None:
        """The user pressed Stop and the run has ended. Close what it opened (the browser, the helper process) and say so."""
        if conv.task_id:
            try:
                await self.orch.cancel(conv.task_id, user_id=conv.user_id)       # closes the browser and records the task as cancelled
            except Exception:
                log.warning("cancel after stop failed", exc_info=False)
        conv.pending, conv.task_id = None, None
        prog.lines.append("⏹ Stopped by you.")
        await prog.flush()                                                       # the last edit has no Stop button
        await self._post(channel, message("Stopped. I closed the browser and kept nothing half-done."))

    async def _outcome(self, conv: Conv, team: str, channel: str, out) -> None:
        conv.task_id = out.task_id
        if out.pending:
            conv.pending = {**out.pending, "task_id": out.task_id}
            if out.pending["type"] == "approval":
                shot = await self.orch.approval_image(out.task_id, conv.user_id)
                if shot:
                    await self._upload(channel, shot, "The page you are approving", "page.png")
            conv.card_ts = await self._post(channel, self.card(conv, out.pending, out.task_id))
            return
        conv.pending, conv.task_id = None, None
        f = out.final or {"kind": "cancelled"}
        await self._post(channel, blocks.final_message(f, out.task_id))
        if f["kind"] == "result":
            await self._remember(conv, team, channel, "assistant", f["summary"])
            await self._attach(channel, f)

    # ───────────────────────── buttons and the code modal ─────────────────────────

    async def on_action(self, *, team: str, channel: str, user: str, action_id: str, task_id: str, message_ts: str) -> None:
        who = await self.links.resolve(team, user)
        if not who:
            return
        conv, _ = await self._conv(team, channel, who)
        conv = self._owning(team, channel, conv, task_id)
        if action_id == "nami_stop":
            if not (conv.busy and conv.run_id == task_id and await self.stop_run(conv)):         # `task_id` carries the run id for this button
                await self._edit(channel, message_ts, message("That request has already finished."))
            return
        if action_id == "nami_ask_cancel":
            if conv.pending and conv.task_id == task_id and not conv.busy:
                await self._edit(channel, message_ts, blocks.decided("Cancelled"))
                await self._cancel(conv, channel)
            else:
                await self._edit(channel, message_ts, message("This card is no longer active."))
            return
        if action_id == "nami_retry":
            if conv.busy or conv.pending:
                return
            if not conv.request:                           # the request text was only in memory
                await self._edit(channel, message_ts, message("I lost that request when I restarted. Please send it again."))
                return
            await self._edit(channel, message_ts, message("Trying again…"))
            await self._start(conv, team, channel, conv.request)
            return
        if action_id not in ACTIONS or conv.busy or not conv.pending or conv.task_id != task_id:
            await self._edit(channel, message_ts, message("This card is no longer active."))
            return
        label, value = ACTIONS[action_id]
        await self._edit(channel, message_ts, blocks.decided(label))        # buttons gone first: no double clicks
        await self._resume(conv, team, channel, value)

    def code_modal(self, channel: str, task_id: str) -> dict:
        return {"type": "modal", "callback_id": "nami_code_modal", "private_metadata": json.dumps({"c": channel, "t": task_id}),
                "title": {"type": "plain_text", "text": "Verification code"}, "submit": {"type": "plain_text", "text": "Send"},
                "blocks": [{"type": "input", "block_id": "code", "label": {"type": "plain_text", "text": "The code you just received"},
                            "element": {"type": "plain_text_input", "action_id": "v", "max_length": 8}}]}

    async def open_code_modal(self, *, team: str, channel: str, user: str, task_id: str, trigger_id: str, message_ts: str = "") -> None:
        who = await self.links.resolve(team, user)
        conv = self._owning(team, channel, (await self._conv(team, channel, who))[0], task_id) if who else None
        if conv and conv.task_id == task_id and conv.pending and conv.pending.get("purpose") == "code":
            conv.card_ts = message_ts or conv.card_ts
            await self.client.views_open(trigger_id=trigger_id, view=self.code_modal(channel, task_id))

    async def submit_code(self, *, team: str, user: str, metadata: str, value: str) -> str | None:
        """Returns an error to show in the modal, or None when the code was taken. The value is held in memory by the
        orchestrator for the next run only; it is not logged, stored or posted."""
        try:
            m = json.loads(metadata)
            channel, task_id = m["c"], m["t"]
        except Exception:
            return "That window is out of date."
        who = await self.links.resolve(team, user)
        conv = self._owning(team, channel, (await self._conv(team, channel, who))[0], task_id) if who else None
        value = (value or "").strip()
        if not who or conv is None or conv.task_id != task_id or not conv.pending or conv.busy:
            return "I'm not waiting for a code any more."
        if not _CODE_OK.fullmatch(value) or not any(c.isdigit() for c in value):
            return "Just the code: 4 to 8 letters or digits."
        if not await self.orch.offer_code(task_id, who.user_id, value):
            return "I'm not waiting for a code any more."
        if conv.card_ts:
            await self._edit(channel, conv.card_ts, blocks.decided("✓ Code sent"))
        self.spawn(self._resume(conv, team, channel, {"code": True}))
        return None

    # ───────────────────────── plumbing ─────────────────────────

    async def _remember(self, conv: Conv, team: str, channel: str, role: str, text: str) -> None:
        ch = await self.stm.ensure_channel(org_id=conv.org_id, kind="slack", external_id=f"{team}:{channel}", name="Nami Slack DM")
        await self.stm.open_thread(channel_id=ch.id, org_id=conv.org_id, user_id=conv.user_id, thread_id=conv.thread_id)
        await self.stm.append(conv.thread_id, role, text, author_user_id=conv.user_id if role == "user" else None)

    async def _history(self, thread_id: str) -> str:
        try:
            ctx = await self.stm.context(thread_id)
        except LookupError:
            return ""
        return "\n".join(f"{t.role}: {t.content}" for t in ctx.recent[-6:])

    async def _post(self, channel: str, msg: dict, **kw) -> str | None:
        try:
            return (await self.client.chat_postMessage(channel=channel, **msg, **NO_PREVIEW, **kw)).get("ts")
        except Exception as e:
            log.warning("could not post to Slack: %s", type(e).__name__)
            return None

    async def _edit(self, channel: str, ts: str, msg: dict) -> None:
        try:
            await self.client.chat_update(channel=channel, ts=ts, **msg, **NO_PREVIEW)
        except Exception as e:
            log.warning("could not edit a Slack message: %s", type(e).__name__)

    async def _upload(self, channel: str, content: bytes | None = None, title: str = "", filename: str = "", path: str | None = None) -> None:
        try:
            if path:
                await self.client.files_upload_v2(channel=channel, file=path, title=title)
            else:
                await self.client.files_upload_v2(channel=channel, content=content, filename=filename, title=title)
        except Exception as e:                           # a picture is a nicety; the answer is already posted
            log.warning("could not upload to Slack: %s", type(e).__name__)

    async def _attach(self, channel: str, f: dict) -> None:
        """Files Nami itself saved for a generation, and the final page picture. Nothing else on disk is ever attached."""
        root = Path(self.orch.api.out_dir).resolve()
        for i, m in enumerate(f.get("media") or [], 1):
            p = m.get("path")
            try:
                ok = bool(p) and Path(p).resolve().is_relative_to(root) and Path(p).is_file()
            except Exception:
                ok = False
            if ok:
                await self._upload(channel, title=f"{m['kind'].capitalize()} {i}", path=str(Path(p).resolve()))
        if f.get("screenshot_b64"):
            await self._upload(channel, base64.b64decode(f["screenshot_b64"]), "Final page", "page.png")

