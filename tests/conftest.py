import os

import fakeredis
import pytest
from redis.asyncio import Redis

from app.core.config import Settings
from app.core.container import build_services
from app.memory.embeddings import HashingEmbedder


@pytest.fixture
def settings(tmp_path):
    return Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db", redis_url=None,
                    stm_window_turns=4, stm_fold_batch=2, stm_channel_window=5)


REAL_REDIS = os.getenv("NAMI_TEST_REDIS_URL")  # e.g. redis://localhost:6390/15 (a db the tests may wipe)


@pytest.fixture
async def redis():
    if not REAL_REDIS:
        yield fakeredis.FakeAsyncRedis()
        return
    r = Redis.from_url(REAL_REDIS, decode_responses=False)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


async def _make(settings, redis, summarizer=None):
    # fakeredis has no Vector Sets, so only real Redis exercises the vectorset backend.
    backend = "vectorset" if (REAL_REDIS and redis is not None) else "memory"
    svc = build_services(settings, redis=redis, embedder=HashingEmbedder(),
                         summarizer=summarizer, semantic_backend=backend)
    await svc.db.create_all()
    return svc


@pytest.fixture
async def svc(settings, redis):
    s = await _make(settings, redis)
    yield s
    await s.db.dispose()


@pytest.fixture
def make_services(settings, redis):
    made = []

    async def factory(summarizer=None, use_redis=True):
        s = await _make(settings, redis if use_redis else None, summarizer)
        made.append(s)
        return s

    yield factory


@pytest.fixture
async def firm(svc):
    """A firm with an admin and a member, plus each user's personal org."""
    admin = await svc.tenancy.ensure_user(google_sub="g-admin", email="admin@acme.in", name="Asha")
    member = await svc.tenancy.ensure_user(google_sub="g-mem", email="ravi@acme.in", name="Ravi")
    org = await svc.tenancy.create_org(name="Acme Foods", owner_id=admin.id)
    await svc.tenancy.add_member(org_id=org.id, user_id=member.id, role="member", actor_id=admin.id)
    return org, admin, member


@pytest.fixture(autouse=True)
async def _stop_shared_playwright():
    yield
    from app.browser import cdp
    await cdp.shutdown()
