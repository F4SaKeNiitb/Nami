"""Agent identity: short-lived, single-use, audience-bound tokens for calls between internal components.

Format follows the SPIFFE JWT-SVID shape (ES256, `sub` = SPIFFE ID, `aud`, `exp`) but the issuer is in-process,
not SPIRE. Each token is minted for ONE target (`aud`), with explicit scopes, lives at most 120 s, and carries a
`jti` that the verifier burns on first use. Verification fails closed: if the replay store cannot be reached the
call is refused.
"""

import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

MAX_TTL_S = 120
AUDIENCES = {"vault", "broker", "browser", "llm-gateway", "memory"}
AGENT_ROLES = {"intent", "access_planner", "planner", "executor", "validator", "verifier", "memory_writer", "broker"}


class IdentityError(Exception):
    """Token missing, invalid, expired, replayed, wrong audience or scope. Never explains which in user-facing text."""


@dataclass(frozen=True)
class Claims:
    sub: str
    aud: str
    org: str
    user: str
    task: str
    role: str
    scopes: tuple[str, ...]
    jti: str
    exp: int


def spiffe_id(trust_domain: str, org: str, role: str, task: str) -> str:
    return f"spiffe://{trust_domain}/org/{org}/agent/{role}/task/{task}"


class ReplayStore(Protocol):
    async def first_use(self, jti: str, ttl_s: int) -> bool: ...


class InMemoryReplayStore:
    def __init__(self) -> None:
        self._seen: dict[str, float] = {}

    async def first_use(self, jti: str, ttl_s: int) -> bool:
        now = time.time()
        self._seen = {k: v for k, v in self._seen.items() if v > now}
        if jti in self._seen:
            return False
        self._seen[jti] = now + ttl_s
        return True


class RedisReplayStore:
    """SET NX EX: exactly one caller wins. Any Redis error propagates, so the verifier fails closed."""

    def __init__(self, redis, prefix: str = "nami"):
        self.redis, self.prefix = redis, prefix

    async def first_use(self, jti: str, ttl_s: int) -> bool:
        return bool(await self.redis.set(f"{self.prefix}:jti:{jti}", b"1", nx=True, ex=ttl_s))


def load_or_create_key(path: str | Path) -> ec.EllipticCurvePrivateKey:
    path = Path(path)
    if path.exists():
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    path.chmod(0o600)
    return key


class IdentityIssuer:
    def __init__(self, key: ec.EllipticCurvePrivateKey, trust_domain: str = "nami.local", kid: str = "nami-1"):
        self._key, self.trust_domain, self.kid = key, trust_domain, kid

    def mint(self, *, org: str, user: str, task: str, role: str, aud: str, scopes: list[str],
             ttl_s: int = 60, now: float | None = None) -> str:
        if aud not in AUDIENCES:
            raise IdentityError("unknown audience")
        if role not in AGENT_ROLES:
            raise IdentityError("unknown agent role")
        if not 0 < ttl_s <= MAX_TTL_S:
            raise IdentityError(f"ttl must be 1..{MAX_TTL_S} seconds")
        t = int(now if now is not None else time.time())
        payload = {"sub": spiffe_id(self.trust_domain, org, role, task), "aud": aud, "iat": t, "exp": t + ttl_s,
                   "jti": uuid.uuid4().hex, "org": org, "user": user, "task": task, "role": role, "scopes": sorted(scopes)}
        return jwt.encode(payload, self._key, algorithm="ES256", headers={"kid": self.kid})

    def jwks(self) -> dict:
        import json
        pub = self._key.public_key()
        jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(pub))
        return {"keys": [{**jwk, "kid": self.kid, "use": "sig", "alg": "ES256"}]}


class IdentityVerifier:
    """Verifies tokens against one or more public keys, selected by the token's `kid` header (key rotation:
    keep the retired key here until the longest token life, 120 s, has passed, then drop it)."""

    def __init__(self, public_key, replay: ReplayStore, trust_domain: str = "nami.local", *, kid: str = "nami-1",
                 extra_keys: dict[str, object] | None = None):
        self._keys: dict[str, object] = {kid: public_key, **(extra_keys or {})}
        self.replay, self.trust_domain = replay, trust_domain

    @classmethod
    def for_issuer(cls, issuer: IdentityIssuer, replay: ReplayStore, *, retired: "list[IdentityIssuer] | None" = None) -> "IdentityVerifier":
        return cls(issuer._key.public_key(), replay, issuer.trust_domain, kid=issuer.kid,
                   extra_keys={r.kid: r._key.public_key() for r in retired or []})

    async def verify(self, token: str | None, *, aud: str, scope: str | None = None, now: float | None = None) -> Claims:
        if not token:
            raise IdentityError("no token")
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError:
            raise IdentityError("invalid token") from None
        pub = self._keys.get(kid)
        if pub is None:
            raise IdentityError("invalid token")                       # unknown or retired key
        try:
            c = jwt.decode(token, pub, algorithms=["ES256"], audience=aud,
                           options={"require": ["exp", "iat", "aud", "sub", "jti"], "verify_exp": now is None},
                           leeway=0)
        except jwt.PyJWTError:
            raise IdentityError("invalid token") from None
        if now is not None and c["exp"] <= now:
            raise IdentityError("invalid token")
        if c["exp"] - c["iat"] > MAX_TTL_S:
            raise IdentityError("invalid token")                       # a token that outlives policy is refused
        if not c["sub"].startswith(f"spiffe://{self.trust_domain}/"):
            raise IdentityError("invalid token")
        if scope is not None and scope not in c.get("scopes", []):
            raise IdentityError("scope not granted")
        try:
            fresh = await self.replay.first_use(c["jti"], max(1, int(c["exp"] - (now or time.time())) + 5))
        except Exception:
            raise IdentityError("replay store unavailable") from None   # fail closed
        if not fresh:
            raise IdentityError("token already used")
        return Claims(c["sub"], c["aud"], c["org"], c["user"], c["task"], c["role"], tuple(c["scopes"]), c["jti"], c["exp"])
