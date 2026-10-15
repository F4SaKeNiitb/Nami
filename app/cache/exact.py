"""Plain key/value cache on Redis.

Every failure degrades to a cache miss. Redis only ever holds data that can be rebuilt,
never the source of truth. Callers put the relevant versions (rules_version,
prompt_version, model) in the key, so changes invalidate by making new keys, not by
scanning and deleting old ones.
"""

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger(__name__)
T = TypeVar("T")


def digest(*parts: Any) -> str:
    raw = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


class ExactCache:
    def __init__(self, redis: Redis | None, prefix: str = "nami", default_ttl_s: int = 3600):
        self.redis = redis
        self.prefix = prefix
        self.default_ttl_s = default_ttl_s
        self.stats: dict[str, dict[str, int]] = {}      # per namespace: hits / misses (for /admin/metrics)

    def _count(self, namespace: str, hit: bool) -> None:
        d = self.stats.setdefault(namespace, {"hits": 0, "misses": 0})
        d["hits" if hit else "misses"] += 1

    def _key(self, namespace: str, key: str) -> str:
        return f"{self.prefix}:{namespace}:{key}"

    async def get_json(self, namespace: str, key: str) -> Any | None:
        if self.redis is None:
            return None
        try:
            raw = await self.redis.get(self._key(namespace, key))
        except RedisError as e:
            log.warning("cache get failed (%s); treating as miss", e)
            self._count(namespace, False)
            return None
        self._count(namespace, raw is not None)
        return None if raw is None else json.loads(raw)

    async def set_json(self, namespace: str, key: str, value: Any, ttl_s: int | None = None) -> None:
        if self.redis is None:
            return
        try:
            await self.redis.set(self._key(namespace, key), json.dumps(value), ex=ttl_s or self.default_ttl_s)
        except RedisError as e:
            log.warning("cache set failed (%s)", e)

    async def delete(self, namespace: str, key: str) -> None:
        if self.redis is None:
            return
        try:
            await self.redis.delete(self._key(namespace, key))
        except RedisError as e:
            log.warning("cache delete failed (%s)", e)

    async def get_or_set(
        self, namespace: str, key: str, factory: Callable[[], Awaitable[T]], ttl_s: int | None = None
    ) -> T:
        hit = await self.get_json(namespace, key)
        if hit is not None:
            return hit
        value = await factory()
        await self.set_json(namespace, key, value, ttl_s)
        return value
