"""Agent identity tokens and the vault gate. Exit 1 on any failure."""

import asyncio
import base64
import json
import tempfile
import time
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.core.config import Settings
from app.core.container import build_services
from app.security.authz import Principal
from app.security.identity import (IdentityError, IdentityIssuer, IdentityVerifier, InMemoryReplayStore, MAX_TTL_S,
                                   load_or_create_key)
from app.security.identity_service import IdentityService
from app.vault.kms import LocalKms
from app.vault.service import VaultError, VaultService

R: list[tuple[str, bool]] = []


def check(name, ok):
    R.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")


async def refused(coro) -> bool:
    try:
        await coro
        return False
    except (IdentityError, VaultError):
        return True


async def main(tmp: Path):
    issuer = IdentityIssuer(load_or_create_key(tmp / "k.pem"))
    replay = InMemoryReplayStore()
    ver = IdentityVerifier.for_issuer(issuer, replay)
    mint = lambda **kw: issuer.mint(**{**dict(org="o1", user="u1", task="t1", role="broker", aud="vault", scopes=["use_credential:c1"]), **kw})  # noqa

    print("tokens")
    c = await ver.verify(mint(), aud="vault", scope="use_credential:c1")
    check("valid token verifies; SPIFFE id has org, role and task", c.sub == "spiffe://nami.local/org/o1/agent/broker/task/t1" and c.user == "u1")
    check("replay: the same token cannot be used twice", await refused(ver.verify(t := mint(), aud="vault")) is False and await refused(ver.verify(t, aud="vault")))
    check("expired token refused", await refused(ver.verify(mint(now=time.time() - 200, ttl_s=60), aud="vault")))
    check("wrong audience refused", await refused(ver.verify(mint(), aud="memory")))
    check("scope not granted refused", await refused(ver.verify(mint(), aud="vault", scope="use_credential:OTHER")))
    tok = mint()
    h, p, s = tok.split(".")
    flipped = ".".join([h, p, s[:-4] + ("AAAA" if s[-4:] != "AAAA" else "BBBB")])
    check("tampered signature refused", await refused(ver.verify(flipped, aud="vault")))
    forged_payload = json.loads(base64.urlsafe_b64decode(p + "=="))
    forged_payload["user"] = "attacker"
    bad_p = base64.urlsafe_b64encode(json.dumps(forged_payload).encode()).rstrip(b"=").decode()
    check("tampered claims refused", await refused(ver.verify(".".join([h, bad_p, s]), aud="vault")))
    other = IdentityIssuer(load_or_create_key(tmp / "other.pem"))
    check("token from a different key refused", await refused(ver.verify(other.mint(org="o1", user="u1", task="t1", role="broker", aud="vault", scopes=[]), aud="vault")))
    none_tok = jwt.encode({"sub": "spiffe://nami.local/x", "aud": "vault", "exp": int(time.time()) + 60, "iat": int(time.time()), "jti": "z"}, None, algorithm="none")
    check("alg=none refused", await refused(ver.verify(none_tok, aud="vault")))
    pub_pem = issuer._key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    import hmac, hashlib
    hdr = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=")
    body = base64.urlsafe_b64encode(json.dumps({"sub": "spiffe://nami.local/x", "aud": "vault", "exp": int(time.time()) + 60, "iat": int(time.time()), "jti": "y2"}).encode()).rstrip(b"=")
    sig = base64.urlsafe_b64encode(hmac.new(pub_pem, hdr + b"." + body, hashlib.sha256).digest()).rstrip(b"=")
    check("algorithm confusion (HS256 signed with the public key) refused", await refused(ver.verify((hdr + b"." + body + b"." + sig).decode(), aud="vault")))
    long_lived = jwt.encode({"sub": "spiffe://nami.local/org/o1/agent/broker/task/t1", "aud": "vault", "iat": int(time.time()), "exp": int(time.time()) + 3600,
                             "jti": "long", "org": "o1", "user": "u1", "task": "t1", "role": "broker", "scopes": []}, issuer._key, algorithm="ES256")
    check("a validly signed token that outlives policy (1 h) refused", await refused(ver.verify(long_lived, aud="vault")))
    check("minting refuses ttl above 120 s", await refused(_async(lambda: mint(ttl_s=MAX_TTL_S + 1))))
    check("minting refuses unknown audience and role", await refused(_async(lambda: mint(aud="database"))) and await refused(_async(lambda: mint(role="root"))))
    check("no token refused", await refused(ver.verify(None, aud="vault")))

    class Down:
        async def first_use(self, jti, ttl):
            raise ConnectionError("redis down")
    check("replay store down fails closed", await refused(IdentityVerifier.for_issuer(issuer, Down()).verify(mint(), aud="vault")))

    jwk = jwt.PyJWKSet.from_dict(issuer.jwks()).keys[0]
    check("JWKS public key verifies our tokens", jwt.decode(mint(), jwk.key, algorithms=["ES256"], audience="vault")["org"] == "o1")
    check("JWKS has no private material", "d" not in issuer.jwks()["keys"][0])

    print("vault gate")
    svc = build_services(Settings(database_url=f"sqlite+aiosqlite:///{tmp}/n.db", redis_url=None))
    await svc.db.create_all()
    a = await svc.tenancy.ensure_user(google_sub="a", email="a@x.com", name="A")
    b = await svc.tenancy.ensure_user(google_sub="b", email="b@x.com", name="B")
    vault = VaultService(svc.db, LocalKms(), verifier=IdentityVerifier.for_issuer(issuer, InMemoryReplayStore()))
    ids = IdentityService(issuer, svc.authz)
    # store while the verifier is attached: store() does not need a token
    c1 = await vault.store(user_id=a.id, kind="password", label="L1", origins=["x.com"], secret={"username": "u", "password": "pw-one-unique"})
    # a second credential of the same user that stays valid next to c1: a second *password* for x.com would (by design) replace c1, so it is another kind
    c2 = await vault.store(user_id=a.id, kind="api_key", label="L2", origins=["x.com"], secret={"key": "key-two-unique-0123456789abcdef"})
    pa = await svc.authz.principal(a.id, a.default_org_id)
    use = lambda cid, tok, user=a.id, task="t1": vault.use(user_id=user, credential_id=cid, origin="https://x.com", actor="agent:broker", task_id=task, token=tok)  # noqa
    tok = lambda cid="": ids.token_for_credential(principal=pa, task_id="t1", credential_id=cid or c1.id, credential_owner_id=a.id)  # noqa

    check("no token: decrypt refused", await refused(use(c1.id, None)))
    t = await tok()
    check("valid token: decrypt works", (await use(c1.id, t))["password"] == "pw-one-unique")
    check("the same token cannot decrypt a second time", await refused(use(c1.id, t)))
    t = await tok()
    check("a token for credential 1 cannot open credential 2", await refused(use(c2.id, t)))
    t = await tok()
    check("a token minted for user A cannot be used as user B", await refused(use(c1.id, t, user=b.id)))
    cb = await vault.store(user_id=b.id, kind="password", label="B1", origins=["x.com"], secret={"username": "b", "password": "pw-bee-unique"})
    wrong_user = issuer.mint(org="o1", user=a.id, task="t1", role="broker", aud="vault", scopes=[f"use_credential:{cb.id}"])
    check("a token issued to user A but scoped to B's own credential is refused for B (token user check)", await refused(use(cb.id, wrong_user, user=b.id)))
    t = await tok()
    check("a token for task t1 cannot be used in task t9", await refused(use(c1.id, t, task="t9")))
    check("no password was decrypted by any refused attempt", all(r.result == "denied" for r in await vault.access_log(a.id) if r.action == "inject" and r.reason == "identity check failed"))
    check("refusals are logged with a reason", sum(r.reason == "identity check failed" for r in await vault.access_log(a.id)) >= 4)

    print("mint after RBAC")
    viewer_org = await svc.tenancy.create_org(name="V", owner_id=b.id)
    await svc.tenancy.add_member(org_id=viewer_org.id, user_id=a.id, role="viewer", actor_id=b.id)
    pv = await svc.authz.principal(a.id, viewer_org.id)
    check("a viewer cannot obtain a token to use a credential", await refused(ids.token_for_credential(principal=pv, task_id="t", credential_id=c1.id, credential_owner_id=a.id)))
    check("a token for someone else's credential is never minted", await refused(ids.token_for_credential(principal=pa, task_id="t", credential_id=c1.id, credential_owner_id=b.id)))
    outsider = Principal(a.id, "some-other-org", None)
    check("a non-member cannot obtain a token", await refused(ids.token_for_credential(principal=outsider, task_id="t", credential_id=c1.id, credential_owner_id=a.id)))
    await svc.close()


async def _async(fn):
    return fn()


with tempfile.TemporaryDirectory() as d:
    asyncio.run(main(Path(d)))
bad = [n for n, ok in R if not ok]
print(f"\n{len(R) - len(bad)}/{len(R)} identity checks passed")
raise SystemExit(1 if bad else 0)
