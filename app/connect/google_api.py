"""Gmail and Drive through Google's API, with the token from "Connect Google". No browser, no password.

Boundaries:
  * a fixed list of operations; URLs are built here, the model only picks an operation and its arguments;
  * hosts are fixed (www.googleapis.com, gmail.googleapis.com), HTTPS only, redirects are not followed;
  * the access token goes in the Authorization header and nowhere else;
  * every response is size-capped, redacted and later passes the prompt-injection gateway: email bodies and documents
    are untrusted text written by other people;
  * `send_email` is never offered to the model. It runs only for a draft the user approved, exactly as approved.
"""

import base64
import io
import json
import re
from dataclasses import dataclass
from email.message import EmailMessage
from html import unescape

import httpx

from app.security.secrets import redact

DRIVE = "https://www.googleapis.com/drive/v3"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
MAX_TEXT = 40_000                 # characters handed back per call
MAX_DOWNLOAD = 15 * 1024 * 1024   # bytes for one file
READ_OPS = ("drive_search", "drive_read", "gmail_search", "gmail_read")
_ID = re.compile(r"^[A-Za-z0-9_-]{6,200}$")
_EMAIL = re.compile(r"^[^@\s,<>\"']+@[^@\s,<>\"']+\.[A-Za-z]{2,}$")
_EXPORT = {"application/vnd.google-apps.document": "text/plain",
           "application/vnd.google-apps.spreadsheet": "text/csv",
           "application/vnd.google-apps.presentation": "text/plain"}


class GoogleApiError(Exception):
    """A reason safe to show the user (never contains the token or a response body)."""


@dataclass(frozen=True)
class Draft:
    to: list[str]
    subject: str
    body: str

    def show(self) -> str:
        return f"To: {', '.join(self.to)}\nSubject: {self.subject}\n\n{self.body}"


def make_draft(to: str, subject: str, body: str) -> Draft:
    """Validate an email draft. Header injection (newlines in To/Subject) and malformed addresses are refused."""
    rcpts = [a.strip() for a in (to or "").split(",") if a.strip()]
    if not rcpts or len(rcpts) > 10 or not all(_EMAIL.match(a) for a in rcpts):
        raise GoogleApiError("the recipient address is not a valid email address")
    if "\n" in subject or "\r" in subject:
        raise GoogleApiError("the subject must be a single line")
    if not body.strip():
        raise GoogleApiError("the email body is empty")
    return Draft(rcpts, subject.strip()[:300], body[:20_000])


class GoogleWorkspace:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.http = client or httpx.AsyncClient(timeout=30, follow_redirects=False)

    async def read(self, op: str, args: dict, *, token: str) -> str:
        if op not in READ_OPS:
            raise GoogleApiError("that is not something I can do with your Google connection")
        fn = getattr(self, "_" + op)
        text = await fn({k: str(v)[:500] for k, v in (args or {}).items()}, token)
        return redact(text)[:MAX_TEXT]

    async def send_email(self, draft: Draft, *, token: str) -> str:
        """Send an approved draft, exactly as approved. Returns the Gmail message id."""
        msg = EmailMessage()
        msg["To"], msg["Subject"] = ", ".join(draft.to), draft.subject
        msg.set_content(draft.body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        r = await self._req("POST", f"{GMAIL}/messages/send", token, json={"raw": raw})
        return str(r.json().get("id", ""))

    # ── Drive ──
    async def _drive_search(self, a: dict, token: str) -> str:
        name = a.get("query", "").replace("\\", "\\\\").replace("'", "\\'")
        q = f"name contains '{name}' and trashed = false" if name else "trashed = false"
        r = await self._req("GET", f"{DRIVE}/files", token, params={
            "q": q, "pageSize": "10", "orderBy": "modifiedTime desc",
            "fields": "files(id,name,mimeType,modifiedTime,size)"})
        files = r.json().get("files", [])
        if not files:
            return "No files found."
        return "\n".join(f"- {f.get('name')} (id: {f.get('id')}, type: {f.get('mimeType')}, modified: {f.get('modifiedTime', '')[:10]})"
                         for f in files)

    async def _drive_read(self, a: dict, token: str) -> str:
        fid = a.get("file_id", "")
        if not _ID.match(fid):
            raise GoogleApiError("that does not look like a Drive file id")
        meta = (await self._req("GET", f"{DRIVE}/files/{fid}", token, params={"fields": "name,mimeType,size"})).json()
        mime, name = meta.get("mimeType", ""), meta.get("name", "")
        if mime in _EXPORT:
            r = await self._req("GET", f"{DRIVE}/files/{fid}/export", token, params={"mimeType": _EXPORT[mime]})
            return f"File: {name}\n\n{r.text}"
        if int(meta.get("size") or 0) > MAX_DOWNLOAD:
            raise GoogleApiError("that file is too large for me to read")
        if mime == "application/pdf" or mime.startswith("text/") or mime in ("application/json",):
            r = await self._req("GET", f"{DRIVE}/files/{fid}", token, params={"alt": "media"})
            body = r.content[:MAX_DOWNLOAD]
            text = _pdf_text(body) if mime == "application/pdf" else body.decode("utf-8", "replace")
            return f"File: {name}\n\n{text}" if text.strip() else f"File: {name}\n\n(no readable text; it may be a scanned image)"
        raise GoogleApiError(f"I can't read files of type {mime or 'unknown'} yet")

    # ── Gmail ──
    async def _gmail_search(self, a: dict, token: str) -> str:
        r = await self._req("GET", f"{GMAIL}/messages", token, params={"q": a.get("query", ""), "maxResults": "10"})
        ids = [m["id"] for m in r.json().get("messages", [])][:10]
        if not ids:
            return "No emails found."
        lines = []
        for mid in ids:
            m = (await self._req("GET", f"{GMAIL}/messages/{mid}", token, params=[
                ("format", "metadata"), ("metadataHeaders", "From"), ("metadataHeaders", "Subject"), ("metadataHeaders", "Date")])).json()
            h = _headers(m)
            lines.append(f"- id: {mid} | from: {h.get('from', '')} | subject: {h.get('subject', '')} | date: {h.get('date', '')} "
                         f"| snippet: {unescape(m.get('snippet', ''))[:200]}")
        return "\n".join(lines)

    async def _gmail_read(self, a: dict, token: str) -> str:
        mid = a.get("message_id", "")
        if not _ID.match(mid):
            raise GoogleApiError("that does not look like an email id")
        m = (await self._req("GET", f"{GMAIL}/messages/{mid}", token, params={"format": "full"})).json()
        h = _headers(m)
        return (f"From: {h.get('from', '')}\nTo: {h.get('to', '')}\nDate: {h.get('date', '')}\nSubject: {h.get('subject', '')}\n\n"
                + _body_text(m.get("payload", {})))

    # ── transport ──
    async def _req(self, method: str, url: str, token: str, **kw) -> httpx.Response:
        if not url.startswith((DRIVE + "/", GMAIL + "/")):
            raise GoogleApiError("that address is not allowed")
        try:
            r = await self.http.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kw)
        except httpx.HTTPError:
            raise GoogleApiError("Google could not be reached") from None
        if r.status_code in (401, 403):
            raise GoogleApiError("Google refused access. The connection may have expired; connect Google again.")
        if r.status_code == 404:
            raise GoogleApiError("Google could not find that item")
        if r.status_code >= 300:
            raise GoogleApiError(f"Google returned an error (HTTP {r.status_code})")
        return r


def _headers(m: dict) -> dict:
    return {h.get("name", "").lower(): h.get("value", "")[:300] for h in m.get("payload", {}).get("headers", [])}


def _body_text(payload: dict) -> str:
    """Prefer text/plain parts; fall back to HTML with the tags stripped. Attachments are not read."""
    plain, html = [], []

    def walk(p: dict) -> None:
        mime, data = p.get("mimeType", ""), (p.get("body") or {}).get("data")
        if data and not p.get("filename"):
            text = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
            (plain if mime == "text/plain" else html if mime == "text/html" else []).append(text)
        for c in p.get("parts", []) or []:
            walk(c)
    walk(payload)
    if plain:
        return "\n".join(plain)
    return re.sub(r"\s+\n", "\n", unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style).*?</\1>", "", "\n".join(html)))))


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((pg.extract_text() or "") for pg in reader.pages[:60])
    except Exception:
        raise GoogleApiError("I could not read that PDF") from None


def parse_json_args(raw: str) -> dict:          # tolerate a model that sends args as a JSON string
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}
