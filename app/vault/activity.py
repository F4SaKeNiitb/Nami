"""The access log in plain language, for the Connections panel. Pure: rows and labels in, sentences out.

Rows never hold a secret, and neither does anything built here. A used API key already has a "request" row that says
what it did, so its decrypt row is hidden unless it was refused.
"""

import re

_STATUS = {401: "the site rejected the key", 403: "the site rejected the key", 402: "the account is out of credits",
           429: "rate limited, try again later"}
_REQ = re.compile(r"^([A-Z]+) (\S*) -> (\d{3}|no response)$")


def _site(origin: str | None) -> str:
    return (origin or "").split("://", 1)[-1]


def _who(actor: str) -> str:
    return "You" if actor == "user" else "Nami"


def describe(row, creds: dict) -> dict | None:
    """One row -> {ts, text, ok, credential_id, action}, or None to hide it. `creds` maps id -> CredentialView."""
    c = creds.get(row.credential_id)
    name = f"“{c.label}”" if c else "a saved item"
    site, ok = _site(row.origin), row.result in ("allowed", "ok")
    a = row.action
    if a == "request":
        m = _REQ.match(row.reason or "")
        method, path, status = m.groups() if m else ("", "", "")
        note = ("it worked" if ok else "couldn't reach it" if status == "no response" else
                _STATUS.get(int(status), f"it returned an error ({status})") if status else "it failed")
        text = f"Used {name} on {site}: {method} {path} ({note})".replace("  ", " ")
    elif a == "inject":
        if c and c.kind == "api_key" and ok:
            return None
        text = (f"Used {name} on {site}" if ok else f"Blocked from using {name} on {site}" + (f" ({row.reason})" if row.reason else ""))
    elif a == "totp":
        text = f"Made a 2FA code from {name}" if ok else f"Blocked from making a 2FA code from {name}" + (f" ({row.reason})" if row.reason else "")
    elif a == "create":
        text = f"{_who(row.actor)} saved {name}"
    elif a == "revoke":
        text = f"{_who(row.actor)} removed {name}" if ok else f"Couldn't remove {name}"
    elif a == "shred":
        text = "Your encryption key was destroyed; saved items can no longer be read"
    else:
        text = f"{a} {name}"
    return {"ts": row.ts.isoformat(), "text": text, "ok": ok, "credential_id": row.credential_id, "action": a}


def build(rows, creds: dict, *, limit: int = 50) -> list[dict]:
    """Newest first."""
    out = [d for d in (describe(r, creds) for r in rows) if d]
    return out[-limit:][::-1]
