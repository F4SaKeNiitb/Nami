"""The vault: the only place a secret is ever encrypted, decrypted or revoked.

Rules enforced here, in code and not in prompts:
  * secrets arrive only through `store` (called from the vault HTTP route, never the chat);
  * `list`/views expose labels and display facts only; there is no method that returns all secrets;
  * `use` decrypts only for a stated origin on the credential's allowlist, and logs every attempt;
  * revoke deletes the ciphertext; destroying a user's key makes everything unreadable.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy import select

from app.db.base import new_id, utcnow
from app.db.engine import Database
from app.db.models import Credential, SecretAccessLog, UserKey
from app.security.identity import IdentityError
from app.security.secrets import contains_secret, redact
from app.telemetry import tel
from app.vault import totp as totp_mod
from app.vault.crypto import DecryptionError, Sealed, build_aad, new_dek, open_sealed, seal
from app.vault.kms import KmsProvider
from app.vault.origins import normalize_allowlist, origin_allowed

KINDS = ("api_key", "password", "oauth", "card", "totp")
_REQUIRED = {
    "api_key": {"key"},
    "password": {"username"},          # the password is optional: phone-number + one-time-code sites have none
    "oauth": {"refresh_token"},
    "card": {"number", "cvv", "exp"},
    "totp": {"seed"},
}
_NEEDS_ORIGIN = {"password", "card", "totp", "oauth"}
_ONE_PER_SITE = {"password", "api_key", "totp"}     # a newer one of these for a site replaces the older. (Cards stay: a site can have several; Google grants have their own flow.)


class VaultError(Exception):
    pass


class NotFound(VaultError):
    pass


class OriginDenied(VaultError):
    pass


class KeyDestroyed(VaultError):
    pass


class SecretPayload(dict):
    """Decrypted fields. Prints as redacted so an accidental log or f-string cannot leak them."""

    def __repr__(self) -> str:
        return "<SecretPayload redacted>"

    __str__ = __repr__


@dataclass(frozen=True)
class CredentialView:
    """Everything the UI and the planner may know about a credential. No secret material."""

    id: str
    kind: str
    label: str
    origins: list[str]
    meta: dict
    created_at: datetime
    last_used_at: datetime | None
    revoked: bool
    replaced: tuple[str, ...] = ()      # ids of older credentials for the same site that saving this one retired (only set by `store`)


def _view(c: Credential) -> CredentialView:
    return CredentialView(c.id, c.kind, c.label, list(c.origin_allowlist), dict(c.meta),
                          c.created_at, c.last_used_at, c.revoked_at is not None)


def _display_meta(kind: str, secret: dict[str, str]) -> dict:
    """Display facts derived from the secret by us, never passed in by the caller."""
    if kind == "api_key":
        return {"last4": secret["key"][-4:]}
    if kind == "password":
        return {"username": secret["username"]}
    if kind == "card":
        digits = re.sub(r"\D", "", secret["number"])
        return {"last4": digits[-4:], "exp": secret["exp"]}
    if kind == "totp":
        return {"digits": int(secret["digits"]), "period": int(secret["period"]), "algorithm": secret["algorithm"]}
    if kind == "oauth":                         # which account and what it may do; never the token
        return {"account": secret.get("account", "")[:200], "scopes": secret.get("scopes", "")[:1000],
                "provider": secret.get("provider", "")[:40]}
    return {}


class VaultService:
    def __init__(self, db: Database, kms: KmsProvider, verifier=None):
        self.db = db
        self.kms = kms
        self.verifier = verifier      # IdentityVerifier; when set, every `use` must carry a valid agent token

    # ───────── keys ─────────

    async def _dek(self, user_id: str, *, create: bool) -> tuple[bytes, int]:
        async with self.db.session() as s:
            row = await s.get(UserKey, user_id)
            if row is None:
                if not create:
                    raise KeyDestroyed("no key for user")
                dek = new_dek()
                s.add(UserKey(user_id=user_id, wrapped_dek=await asyncio.to_thread(self.kms.wrap, dek), kek_id=self.kms.kek_id))
                return dek, 1
            if row.destroyed_at is not None:
                raise KeyDestroyed("user key was destroyed")
            return await asyncio.to_thread(self.kms.unwrap, row.wrapped_dek), row.version

    async def _log(self, *, user_id: str, credential_id: str | None, actor: str, action: str,
                   result: str, origin: str | None = None, task_id: str | None = None,
                   reason: str | None = None) -> None:
        async with self.db.session() as s:
            s.add(SecretAccessLog(user_id=user_id, credential_id=credential_id, actor=actor,
                                  action=action, result=result, origin=origin, task_id=task_id, reason=reason))

    async def log_request(self, *, user_id: str, credential_id: str, actor: str, task_id: str | None, host: str,
                          method: str, path: str, status: int | None) -> None:
        """One outbound request made with a decrypted key: host, method, path (no query, no body) and the status.
        Gives the user "what the key actually did", beside "when it was used"."""
        path = redact(path.split("?", 1)[0].split("#", 1)[0])[:120]
        outcome = "error" if status is None else "ok" if status < 400 else "error"
        await self._log(user_id=user_id, credential_id=credential_id, actor=actor, action="request", result=outcome,
                        origin=f"https://{host}", task_id=task_id,
                        reason=f"{method.upper()} {path} -> {status if status is not None else 'no response'}")

    # ───────── create / read ─────────

    async def store(self, *, user_id: str, kind: str, label: str, secret: dict[str, str],
                    origins: list[str] | None = None, linked_credential_id: str | None = None) -> CredentialView:
        if kind not in KINDS:
            raise VaultError(f"unknown credential kind: {kind}")
        if not _REQUIRED[kind] <= set(secret) or not all(secret[k] for k in _REQUIRED[kind]):
            raise VaultError(f"{kind} needs: {', '.join(sorted(_REQUIRED[kind]))}")
        if contains_secret(label) or len(label) > 120:
            raise VaultError("label must be a short name, not the secret itself")
        secret = {k: v for k, v in secret.items() if v != ""}      # an empty optional field is "not given", not an empty secret
        if kind == "totp":
            p = totp_mod.parse(secret["seed"])
            secret = {"seed": p.seed, "digits": str(p.digits), "period": str(p.period), "algorithm": p.algorithm}
        allow = normalize_allowlist(origins or [])
        if kind in _NEEDS_ORIGIN and not allow:
            raise VaultError(f"{kind} must be tied to at least one website")
        if origins and not allow:
            raise VaultError("no valid website in origins")
        dek, version = await self._dek(user_id, create=True)
        cred = Credential(id=new_id(), user_id=user_id, kind=kind, label=label.strip(), origin_allowlist=allow,
                          ciphertext=b"", nonce=b"", dek_version=version,
                          meta=_display_meta(kind, secret), linked_credential_id=linked_credential_id)
        async with self.db.session() as s:
            if linked_credential_id:
                parent = await s.get(Credential, linked_credential_id)
                if parent is None or parent.user_id != user_id:
                    raise NotFound("linked credential not found")
            sealed = seal(dek, secret, build_aad(user_id=user_id, credential_id=cred.id, kind=kind, origins=allow))
            cred.ciphertext, cred.nonce = sealed.ciphertext, sealed.nonce
            s.add(cred)
            await s.flush()
            replaced = await self._retire_older(s, cred, linked_credential_id) if kind in _ONE_PER_SITE and allow else []
            view = replace(_view(cred), replaced=tuple(replaced))
        await self._log(user_id=user_id, credential_id=view.id, actor="user", action="create", result="allowed")
        for old in replaced:
            await self._log(user_id=user_id, credential_id=old, actor="system", action="revoke", result="allowed",
                            reason="replaced by a newer one for the same site")
        return view

    @staticmethod
    async def _retire_older(s, new: Credential, keep_id: str | None) -> list[str]:
        """Only the latest credential of a kind is kept for a site: older active ones whose sites are all covered by the new one are
        revoked (their ciphertext is deleted, and a 2FA key linked to a revoked login goes with it). A credential that also covers other
        sites is left alone, since its allowlist can't be narrowed without re-sealing it."""
        now, new_sites, gone = utcnow(), set(new.origin_allowlist), []
        olds = (await s.scalars(select(Credential).where(
            Credential.user_id == new.user_id, Credential.kind == new.kind, Credential.revoked_at.is_(None),
            Credential.id != new.id))).all()
        for old in olds:
            if old.id == keep_id or not old.origin_allowlist or not set(old.origin_allowlist) <= new_sites:
                continue
            old.ciphertext, old.nonce, old.revoked_at = b"", b"", now
            gone.append(old.id)
            for child in (await s.scalars(select(Credential).where(Credential.linked_credential_id == old.id,
                                                                   Credential.revoked_at.is_(None)))).all():
                if child.id != new.id:
                    child.ciphertext, child.nonce, child.revoked_at = b"", b"", now
                    gone.append(child.id)
        return gone

    async def list(self, user_id: str, *, include_revoked: bool = False) -> list[CredentialView]:
        async with self.db.session() as s:
            q = select(Credential).where(Credential.user_id == user_id).order_by(Credential.created_at)
            if not include_revoked:
                q = q.where(Credential.revoked_at.is_(None))
            return [_view(c) for c in (await s.scalars(q)).all()]

    async def access_log(self, user_id: str, credential_id: str | None = None) -> list[SecretAccessLog]:
        async with self.db.session() as s:
            q = select(SecretAccessLog).where(SecretAccessLog.user_id == user_id).order_by(SecretAccessLog.ts, SecretAccessLog.id)
            if credential_id:
                q = q.where(SecretAccessLog.credential_id == credential_id)
            return list((await s.scalars(q)).all())

    # ───────── use ─────────

    async def use(self, *, user_id: str, credential_id: str, origin: str, actor: str,
                  task_id: str | None = None, action: str = "inject", token: str | None = None) -> SecretPayload:
        """Decrypt for one use on one origin. Every call, allowed or refused, is logged.

        With a verifier configured, the call also needs a single-use agent token scoped to this credential."""
        async def deny(reason: str, exc: type[VaultError]) -> VaultError:
            tel.inc("secret_injections_total", result="denied")
            with tel.span("vault.use", user_id=user_id, task_id=task_id, **{"vault.action": action, "vault.result": "denied", "vault.reason": reason}):
                pass
            await self._log(user_id=user_id, credential_id=credential_id, actor=actor, action=action,
                            result="denied", origin=origin, task_id=task_id, reason=reason)
            return exc(reason)

        if self.verifier is not None:
            try:
                claims = await self.verifier.verify(token, aud="vault", scope=f"use_credential:{credential_id}")
                if claims.user != user_id or (task_id is not None and claims.task != task_id):
                    raise IdentityError("token belongs to someone else")
            except IdentityError:
                raise await deny("identity check failed", VaultError) from None
        async with self.db.session() as s:
            c = await s.get(Credential, credential_id)
            snapshot = None if c is None else (c.user_id, c.kind, list(c.origin_allowlist),
                                               c.ciphertext, c.nonce, c.revoked_at)
        if snapshot is None or snapshot[0] != user_id:
            raise await deny("not found", NotFound)  # same answer for "other user's" and "missing"
        _, kind, allow, ciphertext, nonce, revoked_at = snapshot
        if revoked_at is not None:
            raise await deny("revoked", NotFound)
        if not origin_allowed(origin, allow):
            raise await deny("origin not allowed", OriginDenied)
        try:
            dek, _ = await self._dek(user_id, create=False)
        except KeyDestroyed:
            raise await deny("key destroyed", KeyDestroyed) from None
        try:
            payload = open_sealed(dek, Sealed(ciphertext, nonce),
                                  build_aad(user_id=user_id, credential_id=credential_id, kind=kind, origins=allow))
        except DecryptionError:
            raise await deny("integrity check failed", VaultError) from None
        async with self.db.session() as s:
            row = await s.get(Credential, credential_id)
            row.last_used_at = utcnow()
        await self._log(user_id=user_id, credential_id=credential_id, actor=actor, action=action,
                        result="allowed", origin=origin, task_id=task_id)
        tel.inc("secret_injections_total", result="allowed")
        with tel.span("vault.use", user_id=user_id, task_id=task_id, **{"vault.action": action, "vault.result": "allowed", "vault.kind": kind}):
            pass
        return SecretPayload(payload)

    async def totp_code(self, *, user_id: str, credential_id: str, origin: str, actor: str,
                        task_id: str | None = None, token: str | None = None, min_left: float = 5.0) -> tuple[str, float]:
        """A 2FA code and how long to wait before typing it. The seed never leaves the vault."""
        p = await self.use(user_id=user_id, credential_id=credential_id, origin=origin, actor=actor,
                           task_id=task_id, action="totp", token=token)
        params = totp_mod.TotpParams(p["seed"], int(p["digits"]), int(p["period"]), p["algorithm"])
        return totp_mod.code_for_fill(params, min_left=min_left)

    # ───────── encrypted blobs (browser session snapshots) ─────────

    async def seal_for_user(self, user_id: str, payload: dict[str, str], aad: bytes) -> Sealed:
        dek, _ = await self._dek(user_id, create=True)
        return seal(dek, payload, aad)

    async def open_for_user(self, user_id: str, sealed: Sealed, aad: bytes) -> SecretPayload:
        dek, _ = await self._dek(user_id, create=False)
        return SecretPayload(open_sealed(dek, sealed, aad))

    # ───────── revoke / shred ─────────

    async def set_auth_scheme(self, *, user_id: str, credential_id: str, scheme: dict | None, actor: str = "system") -> None:
        """Remember (or forget, with None) how this API key is sent. The scheme is a header name or similar, never a secret;
        it is validated by the same parser the executor uses, so only the allowed shapes can be stored."""
        from app.browser.api_auth import SchemeError, parse_scheme
        try:
            clean = parse_scheme(scheme).to_dict() if scheme is not None else None
        except SchemeError:
            raise VaultError("that is not an allowed way to send a key") from None
        async with self.db.session() as s:
            c = await s.get(Credential, credential_id)
            if c is None or c.user_id != user_id or c.kind != "api_key" or c.revoked_at is not None:
                raise NotFound("not found")
            meta = dict(c.meta or {})
            if clean is None:
                meta.pop("auth_scheme", None)
            else:
                meta["auth_scheme"] = clean
            c.meta = meta                                  # a new dict, so the JSON column is saved
        await self._log(user_id=user_id, credential_id=credential_id, actor=actor, action="scheme", result="allowed",
                        reason="forgotten" if clean is None else clean["kind"])

    async def revoke(self, *, user_id: str, credential_id: str, actor: str = "user") -> None:
        async with self.db.session() as s:
            c = await s.get(Credential, credential_id)
            if c is None or c.user_id != user_id:
                found = False
            else:
                found = True
                c.ciphertext, c.nonce, c.revoked_at = b"", b"", utcnow()
                # A TOTP key linked to a revoked password goes with it.
                for child in (await s.scalars(select(Credential).where(
                        Credential.linked_credential_id == credential_id))).all():
                    child.ciphertext, child.nonce, child.revoked_at = b"", b"", utcnow()
        if not found:
            await self._log(user_id=user_id, credential_id=credential_id, actor=actor, action="revoke",
                            result="denied", reason="not found")
            raise NotFound("not found")
        await self._log(user_id=user_id, credential_id=credential_id, actor=actor, action="revoke", result="allowed")

    async def shred_user(self, user_id: str) -> None:
        """Destroy the user's key. Every stored secret becomes permanently unreadable."""
        async with self.db.session() as s:
            row = await s.get(UserKey, user_id)
            if row is not None:
                row.wrapped_dek, row.destroyed_at = b"", utcnow()
            for c in (await s.scalars(select(Credential).where(Credential.user_id == user_id))).all():
                c.ciphertext, c.nonce = b"", b""
                c.revoked_at = c.revoked_at or utcnow()
        await self._log(user_id=user_id, credential_id=None, actor="system", action="shred", result="allowed")
