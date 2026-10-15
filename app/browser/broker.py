"""Credential Broker: the only bridge between the vault and a browser run.

It builds browser-use's `sensitive_data` from credentials the Access Plan approved, one unique
placeholder per value, each bound to its website. The model only ever sees placeholder names.
Every decrypt goes through `vault.use`, so origin binding and the audit log apply.
"""

import asyncio
import logging
from dataclasses import dataclass, field

from app.agent.schemas import AccessPlan
from app.vault.service import VaultService

log = logging.getLogger("nami.broker")


TOTP_MIN_LEFT = 10.0      # seconds a code must still be valid for when it is typed


@dataclass
class BrokerPayload:
    sensitive_data: dict[str, dict[str, str]] = field(default_factory=dict, repr=False)   # {"https://*.site": {name: value}}; repr off so tracebacks/logs cannot print values
    placeholders: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    _values: list[str] = field(default_factory=list, repr=False)

    def redact(self, text: str) -> str:
        """Redaction by value: no model-visible or logged text may contain a secret we hold."""
        for v in sorted(self._values, key=len, reverse=True):
            if v and v in text:
                text = text.replace(v, "[redacted]")
        return text

    def add_one_time(self, site: str, name: str, value: str) -> None:
        """A value the user just gave for this run only (an SMS/email code). Typed through a placeholder like any secret,
        redacted by value, and never stored: the caller builds a fresh payload for every run."""
        self.sensitive_data.setdefault(f"https://*.{site}", {})[name] = value
        self.placeholders.append(name)
        self._values.append(value)

    def prompt_hint(self) -> str:
        if not self.placeholders:
            return ""
        names = ", ".join(f"<secret>{p}</secret>" for p in self.placeholders)
        hint = ("To sign in, type these placeholders exactly; they are replaced with the real values for you: "
                + names + ". You will never see the real values. Only these placeholders exist: never type one you invented. "
                "If the page asks for a verification code (OTP) and none is listed here, type nothing: call request_help(kind='code').")
        if any(p.endswith("2fa_code") for p in self.placeholders):
            hint += " If the site asks for an authenticator code, type the 2fa_code placeholder."
        has_user = any(p.endswith("_username") for p in self.placeholders)
        if has_user and not any(p.endswith("_password") for p in self.placeholders):
            hint += (" No password is saved: this site signs in with the username or phone number and a one-time code it sends."
                     " Type the username placeholder and submit. When the site asks for the code, call request_help(kind='code')."
                     " Never guess a code or ask the user for one yourself.")
        if any(p.endswith(("_password", "_username")) for p in self.placeholders):
            hint += (" If the site says the username or password is wrong, do not submit again: call request_help(kind='login') at once"
                     " (repeated wrong attempts can lock the account).")
        return hint


class Broker:
    def __init__(self, vault: VaultService, *, user_id: str, task_id: str, actor: str = "agent:executor",
                 org_id: str | None = None, identity=None):
        self.vault, self.user_id, self.task_id, self.actor = vault, user_id, task_id, actor
        self.org_id, self.identity = org_id, identity

    async def _use(self, credential_id: str, site: str):
        """Every decrypt gets its own single-use token (when identity is enabled)."""
        token = None
        if self.identity is not None:
            p = await self.identity.authz.principal(self.user_id, self.org_id or "")
            token = await self.identity.token_for_credential(principal=p, task_id=self.task_id, credential_id=credential_id,
                                                             credential_owner_id=self.user_id)
        return await self.vault.use(user_id=self.user_id, credential_id=credential_id, origin=f"https://{site}",
                                    actor=self.actor, task_id=self.task_id, token=token)

    async def _totp_code(self, credential_id: str, site: str, *, min_left: float) -> tuple[str, float]:
        token = None
        if self.identity is not None:
            p = await self.identity.authz.principal(self.user_id, self.org_id or "")
            token = await self.identity.token_for_credential(principal=p, task_id=self.task_id, credential_id=credential_id,
                                                             credential_owner_id=self.user_id)
        return await self.vault.totp_code(user_id=self.user_id, credential_id=credential_id, origin=f"https://{site}",
                                          actor=self.actor, task_id=self.task_id, token=token, min_left=min_left)

    async def vault_use_api_key(self, credential_id: str, host: str):
        """Decrypt one API key for a server-side call to `host` (origin binding and the audit log apply)."""
        return await self._use(credential_id, host)

    async def remember_auth_scheme(self, credential_id: str, scheme: dict | None) -> None:
        """Save (or forget) how this key is sent, so the next task doesn't have to work it out. Best effort."""
        try:
            await self.vault.set_auth_scheme(user_id=self.user_id, credential_id=credential_id, scheme=scheme, actor=self.actor)
        except Exception:
            log.warning("could not save the key's scheme", exc_info=False)

    async def audit_request(self, credential_id: str, host: str, method: str, path: str, status: int | None) -> None:
        """Record what a decrypted key was used for (host, method, path, status). Never blocks or fails the call:
        the decrypt itself was already logged by `vault.use`."""
        try:
            await self.vault.log_request(user_id=self.user_id, credential_id=credential_id, actor=self.actor,
                                         task_id=self.task_id, host=host, method=method, path=path, status=status)
        except Exception:
            log.warning("request audit failed", exc_info=False)

    async def build(self, access: AccessPlan, *, include_cards: bool = False) -> BrokerPayload:
        """include_cards must be True only for an irreversible step the user has just approved."""
        out = BrokerPayload()
        if include_cards:
            for cu in access.cards:
                data = await self._use(cu.credential_id, cu.site)
                bucket = out.sensitive_data.setdefault(f"https://*.{cu.site}", {})
                for field_name, key in (("number", "card_number"), ("exp", "card_exp"), ("cvv", "card_cvv")):
                    name = f"cred_{cu.credential_id[:8]}_{key}"
                    bucket[name] = data[field_name]
                    out.placeholders.append(name)
                    if len(data[field_name]) >= 6:               # redacting a 3-digit CVV by value would mangle text
                        out._values.append(data[field_name])
        if access.login != "password":
            return out
        for cu in access.credentials:
            tag = cu.credential_id[:8]
            bucket = out.sensitive_data.setdefault(f"https://*.{cu.site}", {})
            if cu.kind == "totp":
                # The seed is opened only inside the vault (one logged `totp` use) and only the code comes back: no seed
                # reaches the broker or the agent, standard or not. The name must NOT end in bu_2fa_code, or browser-use
                # would treat the value as a seed and derive its own code. A code can go stale on a long run; min_left
                # leaves room, and a retry builds a fresh one.
                code, wait = await self._totp_code(cu.credential_id, cu.site, min_left=TOTP_MIN_LEFT)
                if wait:
                    await asyncio.sleep(wait)
                name = f"cred_{tag}_2fa_code"
                bucket[name] = code
                out.placeholders.append(name)
                if len(code) >= 6:
                    out._values.append(code)
                continue
            data = await self._use(cu.credential_id, cu.site)
            if cu.kind == "password":
                for field_name in ("username", "password"):
                    if field_name not in data:               # a phone + one-time-code login has no password
                        continue
                    name = f"cred_{tag}_{field_name}"
                    bucket[name] = data[field_name]
                    out.placeholders.append(name)
                    out._values.append(data[field_name])
        return out
