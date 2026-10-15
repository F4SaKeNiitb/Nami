"""The only HTTP path a secret travels. Authenticated with the Chainlit session cookie.

Mounted under /chainlit/vault so the auth cookie (scoped to the Chainlit root path) reaches it.
Secrets arrive in the JSON body of POST /credentials and are never echoed back, logged or put in chat.
"""

import asyncio
import time

from chainlit.auth import get_current_user
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.connect import google_revoke
from app.core.runtime import runtime
from app.security.authz import AuthzService
from app.tenancy.access import Access
from app.vault import activity, totp
from app.vault.service import CredentialView, NotFound, VaultError

router = APIRouter(prefix="/chainlit/vault", tags=["vault"])


async def current_user_id(request: Request, user=Depends(get_current_user)) -> str:
    uid = (getattr(user, "metadata", None) or {}).get("user_id") if user else None
    if not uid:
        raise HTTPException(401, "sign in first")
    return uid


async def same_origin(request: Request, x_requested_with: str | None = Header(None)) -> None:
    """Cheap CSRF guard for state-changing calls: custom header + Origin must match the host."""
    origin = request.headers.get("origin")
    if x_requested_with != "nami" or (origin and origin.split("://", 1)[-1] != request.headers.get("host")):
        raise HTTPException(403, "bad origin")


class NewCredential(BaseModel):
    kind: str
    label: str = Field(max_length=120)
    origins: list[str] = Field(default_factory=list)
    secret: dict[str, str]
    linked_credential_id: str | None = None

    def __repr__(self) -> str:      # keep secrets out of tracebacks and logs
        return f"NewCredential(kind={self.kind!r}, label={self.label!r})"


def _public(v: CredentialView) -> dict:
    return {"id": v.id, "kind": v.kind, "label": v.label, "origins": v.origins, "meta": v.meta,
            "created_at": v.created_at.isoformat(), "last_used_at": v.last_used_at.isoformat() if v.last_used_at else None}


@router.get("/ping")
async def ping(uid: str = Depends(current_user_id)):
    return {"ok": True}


@router.get("/credentials")
async def list_credentials(uid: str = Depends(current_user_id)):
    return [_public(v) for v in await runtime().vault.list(uid)]


@router.post("/credentials", dependencies=[Depends(same_origin)])
async def create_credential(body: NewCredential, uid: str = Depends(current_user_id)):
    d = await Access(runtime().vault.db, AuthzService(runtime().vault.db)).can_create_credential(uid)         # a viewer is read-only; anyone without a membership row keeps working
    if not d.allow:
        raise HTTPException(403, d.reason)
    try:
        v = await runtime().vault.store(user_id=uid, kind=body.kind, label=body.label, secret=body.secret,
                                        origins=body.origins, linked_credential_id=body.linked_credential_id)
    except (VaultError, totp.InvalidTotp) as e:
        raise HTTPException(400, str(e)) from None
    drop_stale_sessions(uid, v)
    return {**_public(v), "replaced": len(v.replaced)} if v.replaced else _public(v)


_bg: set[asyncio.Task] = set()


def drop_stale_sessions(uid: str, view: CredentialView) -> None:
    """A login that replaced an older one may be a different account, and the browser could still be signed in as the old one.
    Clear that site's saved session in the background (it can take a few seconds) so the next task signs in with the new login."""
    if view.kind != "password" or not view.replaced:
        return
    for site in view.origins:
        t = asyncio.create_task(runtime().sessions.revoke(uid, site))
        _bg.add(t)
        t.add_done_callback(_bg.discard)


@router.delete("/credentials/{credential_id}", dependencies=[Depends(same_origin)])
async def revoke_credential(credential_id: str, uid: str = Depends(current_user_id)):
    rt = runtime()
    token, google = None, False
    try:
        view = next((v for v in await rt.vault.list(uid) if v.id == credential_id), None)
        if view is not None and google_revoke.is_google_grant(view):
            google = True
            token = await google_revoke.read_refresh_token(rt.vault, uid, credential_id)    # before the local copy is wiped
        await rt.vault.revoke(user_id=uid, credential_id=credential_id)
    except NotFound:
        raise HTTPException(404, "not found") from None
    if google:                                         # after the local delete, so Google can never hold it up
        return {"revoked": True, "google": await google_revoke.revoke_at_google(rt, uid, token)}
    if view is not None and view.kind == "password":   # a login also keeps a signed-in browser session: remove that too
        sites = list(view.origins)
        cleared = [await rt.sessions.revoke(uid, site) for site in sites]
        return {"revoked": True, "signed_out": sites, "browser_cleared": all(cleared)}
    return {"revoked": True}


class TotpPreview(BaseModel):
    seed: str


@router.post("/totp/preview", dependencies=[Depends(same_origin)])
async def totp_preview(body: TotpPreview, uid: str = Depends(current_user_id)):
    """Show the current code so the user can compare it with their authenticator app. Nothing is stored."""
    try:
        p = totp.parse(body.seed)
    except totp.InvalidTotp as e:
        raise HTTPException(400, str(e)) from None
    return {"code": totp.code_at(p), "seconds_left": int(totp.seconds_left(p)), "standard": p.standard,
            "digits": p.digits, "period": p.period, "now": int(time.time())}


@router.get("/sessions")
async def list_sessions(uid: str = Depends(current_user_id)):
    return sorted(await runtime().sessions.sites(uid))


@router.delete("/sessions/{site}", dependencies=[Depends(same_origin)])
async def revoke_session(site: str, uid: str = Depends(current_user_id)):
    return {"revoked": True, "browser_cleared": await runtime().sessions.revoke(uid, site)}


@router.get("/log")
async def access_log(uid: str = Depends(current_user_id)):
    rows = await runtime().vault.access_log(uid)
    return [{"ts": r.ts.isoformat(), "credential_id": r.credential_id, "action": r.action, "result": r.result,
             "origin": r.origin, "actor": r.actor, "reason": r.reason} for r in rows[-100:]]


@router.get("/activity")
async def activity_feed(credential_id: str | None = None, uid: str = Depends(current_user_id)):
    """The same log as /log in plain sentences, newest first; optionally for one saved item."""
    creds = {c.id: c for c in await runtime().vault.list(uid, include_revoked=True)}
    rows = await runtime().vault.access_log(uid, credential_id)
    return activity.build(rows, creds)
