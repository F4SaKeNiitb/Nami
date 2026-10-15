"""Connect Google: the standard OAuth consent screen, then a refresh token kept encrypted in the vault.

No password is typed anywhere and no browser is automated: the user clicks "Connect Google" in Nami, approves the exact
scopes on Google's own page, and Nami stores only the refresh token (vault: kind "oauth", bound to Google's domains).

Boundaries:
  * `state` is HMAC-signed, names the user, expires in 10 minutes and is single-use; the callback must arrive in the same
    user's signed-in session, so a link cannot be used to attach someone else's Google account;
  * PKCE (S256): the code verifier never leaves the server;
  * scopes are fixed in code: read Drive, read Gmail, send Gmail. Sending still needs an approval card every time;
  * the refresh token goes to the vault and nowhere else (never in chat, logs, events or a model prompt).
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
SCOPES = (
    "openid", "email",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
)
GOOGLE_ORIGINS = ["google.com", "googleapis.com"]      # where the stored token may ever be used
STATE_TTL = 600


class OAuthError(Exception):
    """A reason safe to show the user."""


@dataclass
class Tokens:
    refresh_token: str
    scopes: str
    account: str

    def __repr__(self) -> str:                   # keep the token out of tracebacks and logs
        return f"Tokens(account={self.account!r}, scopes={self.scopes!r})"


class GoogleOAuth:
    def __init__(self, *, client_id: str | None, client_secret: str | None, signing_key: str,
                 client: httpx.AsyncClient | None = None):
        self.client_id, self.client_secret = client_id, client_secret
        self._key = hashlib.sha256(("nami-connect:" + signing_key).encode()).digest()
        self._verifiers: dict[str, tuple[str, float]] = {}       # nonce -> (PKCE verifier, expiry); single replica
        self.http = client or httpx.AsyncClient(timeout=20, follow_redirects=False)

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    # ── step 1: send the user to Google ──
    def start(self, *, user_id: str, redirect_uri: str) -> str:
        if not self.configured:
            raise OAuthError("Google connection is not set up on this server (OAuth client id/secret missing).")
        self._sweep()
        nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(64)
        self._verifiers[nonce] = (verifier, time.time() + STATE_TTL)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        return AUTH_URL + "?" + urlencode({
            "client_id": self.client_id, "redirect_uri": redirect_uri, "response_type": "code",
            "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent", "include_granted_scopes": "false",
            "state": self._sign({"u": user_id, "n": nonce, "e": int(time.time()) + STATE_TTL}),
            "code_challenge": challenge, "code_challenge_method": "S256",
        })

    # ── step 2: Google sends the user back ──
    async def finish(self, *, code: str, state: str, user_id: str, redirect_uri: str) -> Tokens:
        data = self._verify(state)
        if not hmac.compare_digest(data.get("u", ""), user_id or ""):
            raise OAuthError("This connection link belongs to a different Nami account.")
        verifier, _exp = self._verifiers.pop(data["n"], (None, 0))        # single use
        if verifier is None:
            raise OAuthError("This connection link was already used or has expired. Start again.")
        r = await self.http.post(TOKEN_URL, data={
            "code": code, "client_id": self.client_id, "client_secret": self.client_secret,
            "redirect_uri": redirect_uri, "grant_type": "authorization_code", "code_verifier": verifier})
        if r.status_code != 200:
            raise OAuthError("Google did not accept the connection. Please try again.")
        body = r.json()
        refresh = body.get("refresh_token")
        if not refresh:
            raise OAuthError("Google did not return long-term access. Remove Nami at myaccount.google.com/permissions and connect again.")
        granted = set((body.get("scope") or "").split())
        missing = [s.rsplit("/", 1)[-1] for s in SCOPES[2:] if s not in granted]
        if missing:
            raise OAuthError(f"Some permissions were not granted ({', '.join(missing)}). Connect again and tick every box.")
        return Tokens(refresh, " ".join(sorted(granted)), _email_from_id_token(body.get("id_token", "")))

    # ── using and revoking the stored token ──
    async def access_token(self, refresh_token: str) -> str:
        r = await self.http.post(TOKEN_URL, data={"client_id": self.client_id, "client_secret": self.client_secret,
                                                  "refresh_token": refresh_token, "grant_type": "refresh_token"})
        if r.status_code != 200:
            raise OAuthError("Your Google connection has expired or was removed. Connect Google again.")
        return r.json()["access_token"]

    async def revoke(self, refresh_token: str) -> bool:
        """Ask Google to cancel the grant. True only if Google said so; never raises (the local copy is wiped regardless)."""
        try:
            r = await self.http.post(REVOKE_URL, data={"token": refresh_token})
            return r.status_code == 200
        except Exception:
            return False

    # ── internals ──
    def _sign(self, data: dict) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).rstrip(b"=")
        mac = base64.urlsafe_b64encode(hmac.new(self._key, raw, hashlib.sha256).digest()).rstrip(b"=")
        return (raw + b"." + mac).decode()

    def _verify(self, state: str) -> dict:
        try:
            raw, mac = state.encode().split(b".", 1)
            good = base64.urlsafe_b64encode(hmac.new(self._key, raw, hashlib.sha256).digest()).rstrip(b"=")
            if not hmac.compare_digest(mac, good):
                raise ValueError
            data = json.loads(base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4)))
        except Exception:
            raise OAuthError("This connection link is not valid. Start again from Nami.") from None
        if data.get("e", 0) < time.time():
            raise OAuthError("This connection link has expired. Start again from Nami.")
        return data

    def _sweep(self) -> None:
        now = time.time()
        for n, (_v, exp) in list(self._verifiers.items()):
            if exp < now:
                self._verifiers.pop(n, None)


def _email_from_id_token(id_token: str) -> str:
    """Display only (which account is connected). The token came straight from Google's token endpoint over TLS."""
    try:
        payload = id_token.split(".")[1]
        return str(json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("email", ""))[:200]
    except Exception:
        return ""
