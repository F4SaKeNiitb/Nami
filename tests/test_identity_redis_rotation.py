"""Identity: replay store under concurrency (fakeredis, and real Redis when NAMI_TEST_REDIS_URL is set) and key rotation."""

import asyncio
import time

import pytest

from app.security.identity import (IdentityError, IdentityIssuer, IdentityVerifier, InMemoryReplayStore, RedisReplayStore,
                                   load_or_create_key)


def mint(issuer, **kw):
    return issuer.mint(**{**dict(org="o1", user="u1", task="t1", role="broker", aud="vault", scopes=["use_credential:c1"]), **kw})


async def test_exactly_one_of_many_concurrent_verifiers_wins_a_token(redis, tmp_path):
    issuer = IdentityIssuer(load_or_create_key(tmp_path / "k.pem"))
    verifiers = [IdentityVerifier.for_issuer(issuer, RedisReplayStore(redis, "t")) for _ in range(20)]   # separate instances, shared store
    token = mint(issuer)

    async def attempt(v):
        try:
            await v.verify(token, aud="vault")
            return True
        except IdentityError:
            return False
    results = await asyncio.gather(*[attempt(v) for v in verifiers])
    assert sum(results) == 1


async def test_replay_entry_expires_with_the_token(redis, tmp_path):
    store = RedisReplayStore(redis, "t2")
    assert await store.first_use("jti-a", 1) and not await store.first_use("jti-a", 1)
    await asyncio.sleep(1.2)
    assert await store.first_use("jti-a", 1)            # the marker is gone; the token itself would already be expired


async def test_redis_outage_fails_closed(tmp_path):
    class Down:
        async def set(self, *a, **k):
            raise ConnectionError("redis down")
    issuer = IdentityIssuer(load_or_create_key(tmp_path / "k.pem"))
    v = IdentityVerifier.for_issuer(issuer, RedisReplayStore(Down(), "t"))
    with pytest.raises(IdentityError, match="replay"):
        await v.verify(mint(issuer), aud="vault")


# ───────────── key rotation ─────────────

async def test_rotation_old_tokens_verify_while_the_old_key_is_kept_then_stop(tmp_path):
    old = IdentityIssuer(load_or_create_key(tmp_path / "old.pem"), kid="nami-1")
    new = IdentityIssuer(load_or_create_key(tmp_path / "new.pem"), kid="nami-2")
    in_flight, fresh = mint(old), mint(new)
    during = IdentityVerifier.for_issuer(new, InMemoryReplayStore(), retired=[old])          # overlap window
    assert (await during.verify(in_flight, aud="vault")).user == "u1"
    assert (await during.verify(fresh, aud="vault")).user == "u1"
    after = IdentityVerifier.for_issuer(new, InMemoryReplayStore())                           # retired key dropped
    with pytest.raises(IdentityError):
        await after.verify(mint(old), aud="vault")
    assert (await after.verify(mint(new), aud="vault")).user == "u1"


async def test_a_token_cannot_pick_a_key_it_was_not_signed_by(tmp_path):
    good = IdentityIssuer(load_or_create_key(tmp_path / "g.pem"), kid="nami-1")
    attacker = IdentityIssuer(load_or_create_key(tmp_path / "a.pem"), kid="nami-1")           # claims the same kid
    v = IdentityVerifier.for_issuer(good, InMemoryReplayStore())
    with pytest.raises(IdentityError):
        await v.verify(mint(attacker), aud="vault")
    with pytest.raises(IdentityError):                                                        # unknown kid
        await v.verify(mint(IdentityIssuer(attacker._key, kid="nami-99")), aud="vault")


async def test_a_clock_skewed_token_is_refused_without_leeway(tmp_path):
    """Decision: no leeway. Issuer and verifier are the same process, so skew can only mean a forged or stale token."""
    issuer = IdentityIssuer(load_or_create_key(tmp_path / "k.pem"))
    v = IdentityVerifier.for_issuer(issuer, InMemoryReplayStore())
    with pytest.raises(IdentityError):
        await v.verify(mint(issuer, now=time.time() - 61, ttl_s=60), aud="vault")


def test_jwks_lists_the_key_id_and_no_private_material(tmp_path):
    issuer = IdentityIssuer(load_or_create_key(tmp_path / "k.pem"), kid="nami-7")
    k = issuer.jwks()["keys"][0]
    assert k["kid"] == "nami-7" and "d" not in k
