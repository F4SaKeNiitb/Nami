"""WebSocket for the in-page live browser. Mounted under /chainlit so the Chainlit auth cookie reaches it.

A viewer must present BOTH a valid ticket (issued for one task, one user, short-lived) and the signed-in session cookie
of that same user. Anything else is closed before a single frame is sent.
"""

import logging

from fastapi import APIRouter, WebSocket

from app.core.runtime import runtime

log = logging.getLogger("nami.live")
router = APIRouter(prefix="/chainlit/live", tags=["live"])


def user_id_from_cookies(cookies: dict[str, str]) -> str | None:
    try:
        from chainlit.auth import get_token_from_cookies
        from chainlit.auth.jwt import decode_jwt
        token = get_token_from_cookies(cookies)
        return ((decode_jwt(token).metadata or {}).get("user_id")) if token else None
    except Exception:
        return None


@router.websocket("/ws/{ticket}")
async def live_ws(ws: WebSocket, ticket: str):
    origin = ws.headers.get("origin")
    if origin and origin.split("://", 1)[-1] != ws.headers.get("host"):       # no cross-site viewers
        await ws.close(code=4003)
        return
    relay = runtime().live_relay
    t = relay.check(ticket, user_id_from_cookies(dict(ws.cookies)))
    if t is None:
        await ws.close(code=4003)
        return
    await ws.accept()
    await relay.serve(ws, t)
