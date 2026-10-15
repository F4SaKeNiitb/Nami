"""Builds every storage-layer service from settings. One instance per process."""

from dataclasses import dataclass

from redis.asyncio import Redis

from app.cache.exact import ExactCache
from app.cache.llm_cache import LLMCache
from app.cache.redis_client import create_redis
from app.cache.semantic import InMemoryVectorIndex, RedisVectorIndex, SemanticCache, VectorSetIndex
from app.core.config import Settings
from app.db.engine import Database
from app.memory.embeddings import Embedder, default_embedder
from app.memory.long_term import LongTermMemory
from app.memory.short_term import ShortTermMemory, Summarizer
from app.rules.engine import RulesService
from app.security.authz import AuthzService
from app.tenancy.service import TenancyService


@dataclass
class Services:
    db: Database
    redis: Redis | None
    cache: ExactCache
    llm_cache: LLMCache
    tenancy: TenancyService
    authz: AuthzService
    rules: RulesService
    stm: ShortTermMemory
    ltm: LongTermMemory

    async def close(self) -> None:
        if self.redis is not None:
            await self.redis.aclose()
        await self.db.dispose()


def build_services(settings: Settings, *, redis: Redis | None = None, embedder: Embedder | None = None,
                   summarizer: Summarizer | None = None, semantic_backend: str | None = None) -> Services:
    db = Database(settings.database_url)
    redis = redis if redis is not None else create_redis(settings.redis_url)
    embedder = embedder or default_embedder(settings.embedding_model)
    cache = ExactCache(redis, settings.redis_prefix, settings.cache_ttl_s)

    backend = semantic_backend or settings.semantic_cache_backend
    if redis is None:
        backend = "memory"
    if backend == "vectorset":
        index = VectorSetIndex(redis, embedder.dim, embedder.model_name, settings.redis_prefix,
                               settings.semantic_cache_max_entries)
    elif backend == "search":
        index = RedisVectorIndex(redis, embedder.dim, embedder.model_name, settings.redis_prefix)
    elif backend == "memory":
        index = InMemoryVectorIndex()
    else:
        raise ValueError(f"unknown semantic cache backend {backend!r}")
    semantic = SemanticCache(index, embedder, settings.semantic_cache_threshold, settings.semantic_cache_ttl_s)

    return Services(
        db=db, redis=redis, cache=cache,
        llm_cache=LLMCache(cache, semantic, settings.cache_ttl_s),
        tenancy=TenancyService(db),
        authz=AuthzService(db),
        rules=RulesService(db, cache, settings.rules_cache_ttl_s),
        stm=ShortTermMemory(db, redis, settings, summarizer),
        ltm=LongTermMemory(db, embedder, settings),
    )
