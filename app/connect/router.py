"""Connect Google from the website: /chainlit/connect/google/start -> Google's consent page -> /callback.

Under /chainlit so the Chainlit session cookie reaches both ends; the callback must come back to the SAME signed-in user
who started it (checked against the signed state). The refresh token goes straight into the vault.
"""

import logging
from html import escape

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.connect.google_oauth import GOOGLE_ORIGINS, OAuthError
from app.core.runtime import runtime
from app.vault.router import current_user_id

log = logging.getLogger("nami.connect")
router = APIRouter(prefix="/chainlit/connect", tags=["connect"])
CALLBACK = "/chainlit/connect/google/callback"


def redirect_uri(request: Request) -> str:
    base = runtime().settings.public_url or f"{request.url.scheme}://{request.headers.get('host', '')}"
    return base.rstrip("/") + CALLBACK


def _page(title: str, text: str, ok: bool) -> HTMLResponse:
    colour = "#16a34a" if ok else "#d97706"
    return HTMLResponse(status_code=200 if ok else 400, content=f"""<!doctype html><meta charset="utf-8">
<title>{escape(title)}</title><body style="font-family:system-ui;max-width:520px;margin:15vh auto;padding:0 20px">
<h2 style="color:{colour}">{escape(title)}</h2><p>{escape(text)}</p>
<p><a href="/chainlit">Back to Nami</a></p></body>""")


@router.get("/google/start")
async def google_start(request: Request, user_id: str = Depends(current_user_id)):
    try:
        url = runtime().orchestrator.google_oauth.start(user_id=user_id, redirect_uri=redirect_uri(request))
    except OAuthError as e:
        return _page("Can't connect Google", str(e), ok=False)
    return RedirectResponse(url, status_code=302)


@router.get(CALLBACK.removeprefix("/chainlit/connect"))
async def google_callback(request: Request, user_id: str = Depends(current_user_id), code: str = "", state: str = "",
                          error: str = ""):
    if error:
        return _page("Google was not connected", "You cancelled on Google's page, so nothing was saved.", ok=False)
    rt = runtime()
    try:
        tokens = await rt.orchestrator.google_oauth.finish(code=code, state=state, user_id=user_id,
                                                           redirect_uri=redirect_uri(request))
    except OAuthError as e:
        return _page("Google was not connected", str(e), ok=False)
    for old in await rt.vault.list(user_id):                 # one Google connection per user: replace the previous one
        if old.kind == "oauth" and old.meta.get("provider") == "google":
            await rt.vault.revoke(user_id=user_id, credential_id=old.id)
    await rt.vault.store(user_id=user_id, kind="oauth", label=f"Google ({tokens.account or 'account'})",
                         secret={"refresh_token": tokens.refresh_token, "scopes": tokens.scopes, "account": tokens.account,
                                 "provider": "google"},
                         origins=GOOGLE_ORIGINS)
    log.info("google connected for a user (scopes=%d)", len(tokens.scopes.split()))
    return _page("Google connected", f"Nami can now read your Drive and Gmail{' for ' + tokens.account if tokens.account else ''}, "
                 "and draft emails for you to approve. You can close this tab and ask again in Nami. "
                 "Remove it any time under Connections.", ok=True)
