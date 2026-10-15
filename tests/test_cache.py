import numpy as np

from app.cache.llm_cache import LLMCacheScope
from app.cache.semantic import InMemoryVectorIndex, SemanticCache


class FakeEmbedder:
    """Maps known paraphrases to the same direction, so we can test semantic hits exactly."""
    model_name, dim = "fake", 3
    table = {
        "reorder my last swiggy order": [1, 0, 0],
        "repeat my previous swiggy order": [0.98, 0.2, 0],
        "deploy my vercel project": [0, 1, 0],
    }

    async def embed(self, texts):
        v = np.array([self.table.get(t, [0, 0, 1]) for t in texts], dtype=np.float32)
        return v / np.linalg.norm(v, axis=1, keepdims=True)


def scope(**kw):
    base = dict(org_id="o", user_id="u", model="claude-haiku-4-5-20251001", purpose="intent", rules_version="r1")
    return LLMCacheScope(**{**base, **kw})


async def test_exact_cache_roundtrip_and_tenant_isolation(svc):
    c = svc.llm_cache
    await c.store(scope(purpose="title"), "Title for: reorder biryani", "Biryani reorder")
    assert (await c.lookup(scope(purpose="title"), "Title for: reorder biryani")).kind == "exact"
    assert await c.lookup(scope(purpose="title", user_id="other"), "Title for: reorder biryani") is None


async def test_never_caches_secrets_or_live_page_purposes(svc, redis):
    c = svc.llm_cache
    await c.store(scope(), "use key sk-ant-api03-aaaaaaaaaaaaaaaaaaaaaaaa to call", {"x": 1})
    await c.store(scope(purpose="validator"), "is the cart correct?", {"passed": True})
    assert await redis.keys("nami:llm:*") == []


async def test_semantic_hits_paraphrase_within_partition_only(svc):
    from app.cache.llm_cache import LLMCache
    sem = SemanticCache(InMemoryVectorIndex(), FakeEmbedder(), threshold=0.93, ttl_s=60)
    c = LLMCache(svc.cache, sem, ttl_s=60)

    await c.store(scope(), "Reorder my last Swiggy order", {"site": "swiggy.com", "goal": "reorder_last"})
    hit = await c.lookup(scope(), "repeat my previous swiggy order")
    assert hit and hit.kind == "semantic" and hit.similarity > 0.93
    assert await c.lookup(scope(), "deploy my vercel project") is None            # different meaning
    assert await c.lookup(scope(rules_version="r2"), "repeat my previous swiggy order") is None  # rules changed
    assert await c.lookup(scope(user_id="u2"), "repeat my previous swiggy order") is None       # other user
    # summaries and plans are exact-only: a near-match would be a wrong summary / wrong action
    for purpose in ("summary", "plan"):
        await c.store(scope(purpose=purpose), "reorder my last swiggy order", "x")
        assert await c.lookup(scope(purpose=purpose), "repeat my previous swiggy order") is None
        assert await c.lookup(scope(purpose=purpose), "reorder my last swiggy order") is not None


async def test_semantic_entries_expire():
    now = [0.0]
    sem = SemanticCache(InMemoryVectorIndex(clock=lambda: now[0]), FakeEmbedder(), 0.9, ttl_s=10)
    await sem.put("p", "reorder my last swiggy order", 1)
    assert await sem.get("p", "reorder my last swiggy order")
    now[0] = 11
    assert await sem.get("p", "reorder my last swiggy order") is None


async def test_cache_survives_redis_outage(svc):
    class Broken:
        async def get(self, *a, **k):
            from redis.exceptions import ConnectionError
            raise ConnectionError("down")
        set = delete = get
    svc.cache.redis = Broken()
    assert await svc.cache.get_json("x", "y") is None
    await svc.cache.set_json("x", "y", 1)  # no exception
