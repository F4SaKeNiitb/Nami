"""Web pages Slack links to: add a login or key to the vault, and the secure browser window. Mounted under /chainlit so the sign-in
cookie reaches them. (Linking a Slack account has no page: it is done from the Slack profile email, see links.py.)"""

import html
import logging

from chainlit.auth import authenticate_user, get_token_from_cookies
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.tenancy.access import Access
from app.security.authz import AuthzService
from app.vault import totp
from app.vault.service import VaultError

from app.browser.login_probe import LoginShape
from app.core.runtime import runtime
from app.vault.router import drop_stale_sessions, same_origin

router = APIRouter(prefix="/chainlit/slack", tags=["slack"])

# The page scripts are plain strings (not inside an f-string) so quotes and braces reach the browser exactly as written.
# `node --check` on each is how they were verified. If a script ever fails to load, the form posts to a URL that does not accept
# it, so what was typed can never end up in a GET query string, a log or the browser history.
_ADD_JS = """const kind=document.getElementById('kind'),f=document.getElementById('f'),msg=document.getElementById('msg');
const show=()=>document.querySelectorAll('fieldset').forEach(s=>{const on=s.dataset.kind===kind.value;s.hidden=!on;s.querySelectorAll('input').forEach(i=>i.disabled=!on)});
kind.onchange=show;show();
f.onsubmit=async(e)=>{
  e.preventDefault();
  const fields={};
  f.querySelectorAll('fieldset:not([hidden]) input').forEach(i=>{if(i.value)fields[i.name]=i.value});
  const r=await fetch('/chainlit/slack/add',{method:'POST',credentials:'include',headers:{'Content-Type':'application/json','X-Requested-With':'nami'},
    body:JSON.stringify({token:new URLSearchParams(location.search).get('token'),kind:kind.value,fields})});
  const d=await r.json().catch(()=>({}));
  f.querySelectorAll('input:not([type=hidden])').forEach(i=>i.value='');
  if(r.ok){f.hidden=true;msg.textContent=`Saved${d.replaced?' (it replaced your older one for this site)':''}. Go back to Slack and press "I've added it".`}else msg.textContent=d.detail||'That did not work.';
};"""


class HideQuery(logging.Filter):
    """Keeps query strings of Slack pages out of the access log. Their tokens are one-time, but nothing typed in a form should ever be
    one log line away from a reader, whatever a page does."""

    def filter(self, record: logging.LogRecord) -> bool:
        a = record.args
        if isinstance(a, tuple) and len(a) >= 3 and isinstance(a[2], str) and a[2].startswith("/chainlit/slack/") and "?" in a[2]:
            record.args = (*a[:2], a[2].split("?", 1)[0] + "?<hidden>", *a[3:])
        return True

_PAGE = """<!doctype html><meta charset=utf-8><title>Nami</title>
<body style="font-family:system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem">{body}</body>"""


async def optional_user(request: Request):
    """The signed-in Chainlit user from the auth cookie, or None. (Chainlit's own dependency answers 401 for a visitor who is not
    signed in; this page wants to show them a friendly note instead, so it reads the same cookie and checks it the same way.)"""
    token = get_token_from_cookies(request.cookies)
    if not token:
        return None
    try:
        return await authenticate_user(token)
    except HTTPException:
        return None


# ───────────────────────── a one-time page to add exactly what a task needs ─────────────────────────

_FIELDS = {"password": ("username", "password"), "api_key": ("key",)}      # what each kind may carry from this page
_NAMES = {"password": "Website login", "api_key": "API key"}


def _form(req, shape) -> str:
    """Only what this site's sign-in page was seen to ask for (see browser/login_probe.py). If it could not be looked at, the usual
    fields, with the password marked optional because some sites only send a code."""
    primary = [k for k in ("api_key", "password") if k in req.kinds]
    who, kind = {"phone": ("Phone number", "tel"), "email": ("Email", "email"), "username": ("Username", "text")}.get(
        shape.identifier, ("Username, email or phone number", "text"))
    login = [("username", who, kind, True)]
    if not (shape.observed and shape.password is False):             # seen to have no password box: don't ask for one
        sure = bool(shape.observed and shape.password)
        login.append(("password", "Password" if sure else "Password (leave empty if the site only sends a one-time code)", "password", sure))
        if "totp" in req.kinds:
            login.append(("twofa", "Authenticator (2FA) setup key (optional)", "password", False))
    fields = {"password": login, "api_key": [("key", "API key", "password", True)]}
    groups = "".join(
        f"<fieldset data-kind='{k}' {'hidden' if i else ''}>" + "".join(
            f"<label>{html.escape(lab)}<input name='{n}' type='{t}' autocomplete='off' {'required' if need else ''}></label>"
            for n, lab, t, need in fields[k]) + "</fieldset>" for i, k in enumerate(primary))
    pick = ("<label>Save as <select id=kind>" + "".join(f"<option value='{k}'>{_NAMES[k]}</option>" for k in primary) + "</select></label>") if len(primary) > 1 else "<input type=hidden id=kind value='%s'>" % primary[0]
    return pick + groups


@router.get("/add", response_class=HTMLResponse)
async def add_page(token: str = "", user=Depends(optional_user)):
    req = runtime().slack_links.peek_add(token)
    if not req:
        return _PAGE.format(body="<h2>This link has expired</h2><p>Ask Nami in Slack again and it will send a new one.</p>")
    uid = (getattr(user, "metadata", None) or {}).get("user_id") if user else None
    if not uid:
        return _PAGE.format(body="<h2>Sign in to Nami first</h2><p>Open <a href='/chainlit'>Nami</a>, sign in with Google, then click the link from Slack again.</p>")
    if uid != req.user_id:
        return _PAGE.format(body="<h2>This link is for a different Nami account</h2><p>Sign in as the account you linked to Slack.</p>")
    if not any(k in req.kinds for k in ("password", "api_key")):
        return _PAGE.format(body="<h2>Nothing to add here</h2><p>Go back to Slack.</p>")
    site = html.escape(req.site)
    shape = await runtime().slack_links.shape_of(token) or LoginShape()
    seen = html.escape(shape.summary(req.site)) if "password" in req.kinds else ""
    return _PAGE.format(body=f"""<h2>Add to your vault: {site}</h2>
<p><i>{seen}</i></p>
<p>Saved encrypted and usable <b>only on {site}</b>. It goes straight to the vault; it never passes through Slack or the model.</p>
<form id=f method=post action="/chainlit/slack/add/no-script" style="display:grid;gap:.6rem">{_form(req, shape)}<button style="padding:.6rem 1rem">Save securely</button></form><p id=msg></p>
<style>label{{display:grid;gap:.2rem}}input,select{{padding:.5rem}}fieldset{{border:0;padding:0;display:grid;gap:.6rem}}</style>
<script>{_ADD_JS}</script>""")


class AddBody(BaseModel):
    token: str
    kind: str
    fields: dict[str, str]

    def __repr__(self) -> str:      # keep secrets out of tracebacks and logs
        return f"AddBody(kind={self.kind!r})"


@router.post("/add", dependencies=[Depends(same_origin)])
async def add_credential(body: AddBody, user=Depends(optional_user)):
    """Saves into the vault for the token's user and site only. The page cannot choose the site, the owner or an unlisted kind."""
    rt = runtime()
    uid = (getattr(user, "metadata", None) or {}).get("user_id") if user else None
    req = rt.slack_links.peek_add(body.token)
    if not uid:
        raise HTTPException(401, "sign in first")
    if not req or req.user_id != uid:
        raise HTTPException(400, "this link has expired")
    if body.kind not in _FIELDS or body.kind not in req.kinds:
        raise HTTPException(400, "that isn't what I asked for")
    extra = set(body.fields) - set(_FIELDS[body.kind]) - ({"twofa"} if body.kind == "password" and "totp" in req.kinds else set())
    if extra:
        raise HTTPException(400, "unexpected field")
    if not (await Access(rt.vault.db, AuthzService(rt.vault.db)).can_create_credential(uid)).allow:
        raise HTTPException(403, "you can't add credentials")
    secret = {k: v for k, v in body.fields.items() if k in _FIELDS[body.kind] and v != ""}
    try:
        v = await rt.vault.store(user_id=uid, kind=body.kind, label=f"{req.site} {_NAMES[body.kind].lower()}", secret=secret, origins=[req.site])
        if body.fields.get("twofa", "").strip():
            await rt.vault.store(user_id=uid, kind="totp", label=f"{req.site} 2FA", secret={"seed": body.fields["twofa"].strip()},
                                 origins=[req.site], linked_credential_id=v.id)
    except (VaultError, totp.InvalidTotp) as e:
        raise HTTPException(400, str(e)) from None
    rt.slack_links.finish_add(body.token)
    drop_stale_sessions(uid, v)
    return {"saved": True, "replaced": len(v.replaced)}


# ───────────────────────── the secure window for a step only the user can do ─────────────────────────

@router.get("/solve", response_class=HTMLResponse)
async def solve_page(token: str = "", user=Depends(optional_user)):
    """Shows the task's live browser (a hosted browser's own live view) to the task's owner, for a CAPTCHA or passkey. The browser link
    is never sent through Slack: Slack only gets this one-time page link, which opens only for the signed-in owner of the task."""
    rt = runtime()
    req = rt.slack_links.peek_solve(token)
    if not req:
        return _PAGE.format(body="<h2>This link has expired</h2><p>Ask Nami in Slack again.</p>")
    uid = (getattr(user, "metadata", None) or {}).get("user_id") if user else None
    if not uid:
        return _PAGE.format(body="<h2>Sign in to Nami first</h2><p>Open <a href='/chainlit'>Nami</a>, sign in with Google, then click the link from Slack again.</p>")
    if uid != req.user_id:
        return _PAGE.format(body="<h2>This link is for a different Nami account</h2><p>Sign in as the account you linked to Slack.</p>")
    info = await rt.orchestrator.surface(req.task_id, uid)
    url = (info or {}).get("live_view_url") or ""
    if not info:
        return _PAGE.format(body="<h2>That window isn't available any more</h2><p>The task moved on or was cancelled. Go back to Slack.</p>")
    if not url.startswith("https://"):
        return _PAGE.format(body="<h2>This server shows the window inside the Nami chat</h2><p>Open <a href='/chainlit'>Nami</a> and continue there.</p>")
    site = html.escape(req.site)
    return (f"<!doctype html><meta charset=utf-8><title>Secure window</title><body style='font-family:system-ui;margin:0'>"
            f"<div style='padding:.6rem 1rem'><b>Secure window for {site}.</b> Do just the step Nami asked for (for example the CAPTCHA). "
            f"Nami can't see or type anything while you do. When you're finished, go back to Slack and press <b>I'm done</b>.</div>"
            f"<iframe src='{html.escape(url, quote=True)}' allow='clipboard-read; clipboard-write' "
            f"style='border:0;width:100%;height:calc(100vh - 4.5rem)'></iframe></body>")
