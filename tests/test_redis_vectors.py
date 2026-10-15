"""Integration tests against a real Redis 8. Skipped unless NAMI_TEST_REDIS_URL is set:

    redis-server --port 6390 --save '' --appendonly no --daemonize yes
    NAMI_TEST_REDIS_URL=redis://localhost:6390/15 uv run pytest
"""

import os

import numpy as np
import pytest
from redis.asyncio import Redis

from app.cache.semantic import RedisVectorIndex, SemanticCache, VectorSetIndex

URL = os.getenv("NAMI_TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(not URL, reason="set NAMI_TEST_REDIS_URL to a Redis 8 instance")


class Emb:
    model_name, dim = "itest", 3
    table = {"reorder my last swiggy order": [1, 0, 0], "repeat my previous swiggy order": [0.98, 0.2, 0],
             "deploy my vercel project": [0, 1, 0]}

    async def embed(self, texts):
        v = np.array([self.table.get(t, [0, 0, 1]) for t in texts], dtype=np.float32)
        return v / np.linalg.norm(v, axis=1, keepdims=True)


@pytest.fixture
async def r():
    client = Redis.from_url(URL, decode_responses=False)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


async def test_vectorset_roundtrip_isolation_and_cosine(r):
    cache = SemanticCache(VectorSetIndex(r, 3, "itest"), Emb(), threshold=0.93, ttl_s=60)
    await cache.put("a" * 32, "reorder my last swiggy order", {"goal": "reorder"})
    hit = await cache.get("a" * 32, "repeat my previous swiggy order")
    assert hit and hit.response == {"goal": "reorder"}
    assert abs(hit.similarity - 0.9798) < 1e-3          # true cosine, not VSIM's (1+cos)/2
    assert await cache.get("a" * 32, "deploy my vercel project") is None   # cosine 0 → miss
    assert await cache.get("b" * 32, "repeat my previous swiggy order") is None  # other partition
    assert await r.ttl(b"nami:sempl:" + (await r.zrange(b"nami:semvs-exp:itest-3:" + b"a" * 32, 0, 0))[0]) > 0


async def test_vectorset_expiry_and_cap(r):
    now = [1_000_000.0]
    idx = VectorSetIndex(r, 3, "itest", max_entries=2, clock=lambda: now[0])
    cache = SemanticCache(idx, Emb(), 0.9, ttl_s=10)
    await cache.put("p" * 32, "reorder my last swiggy order", 1)
    now[0] += 11                                          # logically expired
    assert await cache.get("p" * 32, "reorder my last swiggy order") is None
    for _ in range(3):                                    # cleanup drops the stale entry and caps at 2
        await cache.put("p" * 32, "deploy my vercel project", 2)
    assert await r.vset().vcard("nami:semvs:itest-3:" + "p" * 32) == 2
    assert await r.zcard("nami:semvs-exp:itest-3:" + "p" * 32) == 2


async def test_missing_partition_is_a_miss(r):
    cache = SemanticCache(VectorSetIndex(r, 3, "itest"), Emb(), 0.9, 60)
    assert await cache.get("z" * 32, "reorder my last swiggy order") is None


async def test_query_engine_backend_if_available(r):
    try:
        await r.execute_command("FT._LIST")
    except Exception:
        pytest.skip("this Redis build has no Query Engine (FT.*); vectorset backend is used instead")
    cache = SemanticCache(RedisVectorIndex(r, 3, "itest", prefix="nami-test"), Emb(), 0.93, 60)
    await cache.put("a" * 32, "reorder my last swiggy order", {"goal": "reorder"})
    assert (await cache.get("a" * 32, "repeat my previous swiggy order")).response == {"goal": "reorder"}
