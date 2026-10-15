"""Pure functions that turn what the orchestrator returns into Slack messages (Block Kit). Nothing here talks to Slack.

Everything that came from a web page or a model goes through `esc` (Slack treats <, > and & as markup), and every message that
carries text is also given plain `text` for notifications. No secret ever reaches here: results and pauses are already redacted.
"""

import re

from app.security.secrets import redact

LIMIT = 2900              # Slack caps a section's text at 3000


def esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def mrkdwn(s: str) -> str:
    """Escape, then turn **bold** into Slack's *bold*."""
    return re.sub(r"\*\*(.+?)\*\*", r"*\1*", esc(s))


def clip(s: str, n: int = LIMIT) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": clip(text)}}


def _button(label: str, action_id: str, task_id: str, style: str | None = None) -> dict:
    b = {"type": "button", "text": {"type": "plain_text", "text": label}, "action_id": action_id, "value": task_id}
    if style:
        b["style"] = style
    return b


def _actions(*buttons: dict) -> dict:
    return {"type": "actions", "elements": list(buttons)}


def message(text: str, *blocks: dict) -> dict:
    """The notification text is redacted here, so no message built through this can carry a secret-shaped string into a push preview."""
    return {"text": clip(redact(text)), "blocks": list(blocks) or [_section(redact(text))]}


# ───────────────────────── pauses ─────────────────────────

def pending_message(p: dict, task_id: str, web_url: str, add_url: str | None = None, window_url: str | None = None) -> dict:
    kind = p["type"]
    if kind == "ask_user" and p.get("purpose") == "code":
        return message(p["question"], _section(mrkdwn(p["question"]) + "\n_Use the button so the code isn't saved in this conversation._"),
                       _actions(_button("Enter code", "nami_code", task_id, "primary"), _button("Cancel", "nami_code_cancel", task_id)))
    if kind == "ask_user":
        return message(p["question"], _section(mrkdwn(p["question"])), _actions(_button("Cancel", "nami_ask_cancel", task_id)))
    if kind == "need_credentials":
        notes = [n for n in p.get("notes", []) if n not in p["reason"]]          # the reason often already says it
        how = (f"<{add_url}|*Add it securely*> opens a small page that asks only for what this needs. " if add_url
               else f"Add it in the <{web_url}|Nami web app> (*Add a login or API key*). ")
        body = (f"{mrkdwn(p['reason'])}\n" + (mrkdwn("\n".join(notes)) + "\n" if notes else "") +
                f"\n{how}It goes straight to the encrypted vault and never through Slack. Then press the button below.")
        return message(p["reason"], _section(body.strip()),
                       _actions(_button("I've added it", "nami_added", task_id, "primary"), _button("Cancel", "nami_creds_cancel", task_id)))
    if kind == "approval":
        lines = [f"*Before I do this:* {esc(p['subgoal'])}"]
        if p.get("summary"):
            lines.append(esc(p["summary"]))
        lines.append(f"_{esc(p['reason'].capitalize())}._")
        if p.get("amount") is not None and p.get("currency"):
            lines.append(f"*Total: {esc(p['currency'])} {p['amount']:g}*")
        if p.get("pay_with"):
            lines.append(f"Pay with: {esc(p['pay_with'])}")
        if p.get("warning"):
            lines.append(f":warning: {esc(p['warning'])}")
        return message(f"Approval needed: {p['subgoal']}", _section("\n".join(lines)),
                       _actions(_button("Approve", "nami_approve", task_id, "primary"), _button("Cancel", "nami_deny", task_id, "danger")))
    # takeover: a step only the user can do (a CAPTCHA, a passkey). The browser itself is never put in Slack.
    why = esc(p.get("reason", "this step needs you"))
    if window_url:
        return message(f"I'm stuck: {p.get('reason', '')}", _section(
            f"I'm stuck on a step I can't do for you: {why}\n\n<{window_url}|*Open the secure window*> and do just that step yourself "
            "(for example, solve the CAPTCHA). I can't see or type anything while you do. Then come back here and press *I'm done*."),
            _actions(_button("I'm done", "nami_takeover_done", task_id, "primary"), _button("Cancel", "nami_takeover_cancel", task_id)))
    return message(f"I'm stuck: {p.get('reason', '')}", _section(
        f"I'm stuck on a step I can't do for you: {why}\n\nThat one needs a browser window and I can't open one from Slack here. "
        f"Do it yourself on the site, or start the task in the <{web_url}|Nami web app>."),
        _actions(_button("Cancel", "nami_takeover_cancel", task_id)))


def decided(label: str) -> dict:
    """What a card turns into once it has been answered, so it can't be clicked twice."""
    return message(label)


# ───────────────────────── results ─────────────────────────

def final_message(f: dict, task_id: str | None = None) -> dict:
    k = f["kind"]
    if k == "result":
        head = ":white_check_mark:" if f.get("verified") else ":warning:"
        out = [f"{head} {mrkdwn(redact(f['summary']))}"]
        facts = f.get("facts") or {}
        if facts:
            out.append("\n".join(f"• *{esc(redact(str(a)))}:* {esc(redact(str(b)))}" for a, b in facts.items()))
        if f.get("content"):
            out.append(esc(redact(str(f["content"]))))
        if f.get("unverified"):
            out.append("_I couldn't check:_ " + "; ".join(esc(redact(str(u))) for u in f["unverified"]))
        if not f.get("verified"):
            out.append("_I couldn't fully verify this._")
        for i, m in enumerate(f.get("media") or [], 1):
            if m.get("url", "").startswith("https://"):
                out.append(f"<{m['url']}|{m['kind'].capitalize()} {i}>")
        if f.get("url", "").startswith("https://"):
            out.append(f"<{f['url']}|Open the page>")
        return message(f["summary"], *[_section(t) for t in _chunks("\n\n".join(out))])
    if k == "failed":
        return message(f"I couldn't finish: {f['reason']}", _section(f"I couldn't finish this: {esc(f['reason'])}"),
                       _actions(_button("Try again", "nami_retry", task_id or "")))
    if k == "connect_google":
        return message(f.get("message", ""), _section(f"{mrkdwn(f.get('message', ''))}\n\nConnect Google in the Nami web app, then ask me again."))
    if k == "secret_refused":
        return message("That contains a secret.", _section("That contains a secret. Please use the Nami web app's secure form instead."))
    if k in ("blocked", "unsupported", "error"):
        return message(f.get("message", "I can't do that."))
    return message(f.get("message") or "Okay, stopped.")


def _chunks(text: str, n: int = LIMIT) -> list[str]:
    return [text[i:i + n] for i in range(0, len(text), n)] or [""]


# ───────────────────────── progress, linking, help ─────────────────────────

def progress_message(lines: list[str], working: bool = True, keep: int = 14, stop_value: str | None = None) -> dict:
    """While the work is running the message carries a Stop button; the last edit (working=False) has none, so it can't be pressed late."""
    shown = lines[-keep:]
    body = "\n".join(esc(re.sub(r"\*\*(.+?)\*\*", r"\1", ln)) for ln in shown)
    head = ":hourglass_flowing_sand: *Working on it…*" if working else "*What I did*"
    extra = [_actions(_button("Stop", "nami_stop", stop_value, "danger"))] if (working and stop_value) else []
    return message(head, _section(f"{head}\n{body}"), *extra)


HELP = ("Tell me what to do on the web, in plain words, for example _check my OpenRouter credits_.\n"
        "• *Approvals* and *verification codes* come back here as buttons.\n"
        "• *Passwords and API keys* are added in the Nami web app, never in Slack.\n"
        "• Press *Stop* on the progress message (or say *cancel*) to stop what I'm doing; *unlink* disconnects this Slack account; *help* shows this again.\n"
        "I only work in direct messages, because results can include personal details.")
