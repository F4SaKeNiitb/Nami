"""Disconnect Google for real: besides wiping Nami's copy, tell Google to cancel the grant and forget the saved sign-in.

Best effort and never in the way: the local delete has already happened by the time this runs, a slow or failing Google
only means the grant stays listed at myaccount.google.com/permissions, and the token is never logged or returned.
"""

import asyncio
import logging

from app.connect.google_oauth import GOOGLE_ORIGINS

log = logging.getLogger("nami.connect")
REVOKE_TIMEOUT = 5.0


def is_google_grant(view) -> bool:
    return view.kind == "oauth" and (view.meta or {}).get("provider") == "google"


async def read_refresh_token(vault, user_id: str, credential_id: str) -> str | None:
    """Open the token while it still exists (call BEFORE the local revoke). Logged by the vault like any other use."""
    try:
        data = await vault.use(user_id=user_id, credential_id=credential_id, origin=f"https://{GOOGLE_ORIGINS[0]}", actor="user")
        return data["refresh_token"]
    except Exception:
        log.warning("could not read the Google token for revoke", exc_info=False)
        return None


async def revoke_at_google(rt, user_id: str, refresh_token: str | None) -> dict:
    """Cancel the grant at Google and drop the saved Google browser session. Each step fails on its own."""
    out = {"grant": False, "session": False}
    if refresh_token:
        try:
            out["grant"] = bool(await asyncio.wait_for(rt.orchestrator.google_oauth.revoke(refresh_token), REVOKE_TIMEOUT))
        except Exception:
            log.warning("Google revoke call failed", exc_info=False)           # no token in the message
    try:
        await rt.sessions.revoke(user_id, GOOGLE_ORIGINS[0])
        out["session"] = True
    except Exception:
        log.warning("dropping the saved Google session failed", exc_info=False)
    return out
