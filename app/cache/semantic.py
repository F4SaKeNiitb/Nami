"""Semantic cache: reuse an earlier answer when a new prompt *means* the same thing.

Entries live in a "partition" (a hash of tenant + model + purpose + rules/prompt
versions). A lookup can only ever match inside one user's partition, under the same rules.

Backends:
  VectorSetIndex       default. Redis 8 core Vector Sets (VADD/VSIM): one vector set per
                       partition, so tenant isolation is structural. Works on any Redis 8,
                       including Homebrew's build and the redis:8 image.
  RedisVectorIndex     Redis Query Engine (FT.* HNSW index). Only on builds that ship the
                       search module (redis:8 Docker image, Redis Stack, Azure Managed Redis).
  InMemoryVectorIndex  dev/tests, or when Redis is unavailable.
Azure Cache for Redis Basic/Standard has neither; deploy redis:8 as a sidecar.
"""

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np
from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError

from app.db.base import new_id
from app.memory.embeddings import Embedder

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SemanticHit:
    response: Any
    similarity: float
    matched_text: str


class VectorIndex(Protocol):
    async def search(self, partition: str, vector: np.ndarray) -> tuple[float, dict] | None: ...
    async def add(self, partition: str, vector: np.ndarray, payload: dict, ttl_s: int) -> None: ...


@dataclass
class _Entry:
    vector: np.ndarray
    payload: dict
    expires_at: float


@dataclass
class InMemoryVectorIndex:
    clock: Callable[[], float] = time.monotonic
    _data: dict[str, list[_Entry]] = field(default_factory=dict)

    async def search(self, partition: str, vector: np.ndarray) -> tuple[float, dict] | None:
        now = self.clock()
        live = [e for e in self._data.get(partition, []) if e.expires_at > now]
        self._data[partition] = live
        if not live:
            return None
        sims = np.stack([e.vector for e in live]) @ vector
        i = int(np.argmax(sims))
        return float(sims[i]), live[i].payload

    async def add(self, partition: str, vector: np.ndarray, payload: dict, ttl_s: int) -> None:
        self._data.setdefault(partition, []).append(_Entry(vector, payload, self.clock() + ttl_s))


class RedisVectorIndex:
    def __init__(self, redis: Redis, dim: int, model_name: str, prefix: str = "nami"):
        slug = re.sub(r"[^a-z0-9]+", "-", model_name.lower()).strip("-")
        # Dimension and model are in the name, so changing the embedder starts a fresh index.
        self.index = f"{prefix}-semcache-{slug}-{dim}"
        self.key_prefix = f"{prefix}:semcache:{slug}-{dim}:"
        self.redis, self.dim = redis, dim
        self._ready = False

    async def _ensure(self) -> None:
        if self._ready:
            return
        from redis.commands.search.field import TagField, TextField, VectorField
        from redis.commands.search.index_definition import IndexDefinition, IndexType

        ft = self.redis.ft(self.index)
        try:
            await ft.info()
        except ResponseError:
            try:
                await ft.create_index(
                    (
                        TagField("partition"),
                        TextField("payload", no_index=True),
                        VectorField("embedding", "HNSW",
                                    {"TYPE": "FLOAT32", "DIM": self.dim, "DISTANCE_METRIC": "COSINE"}),
                    ),
                    definition=IndexDefinition(prefix=[self.key_prefix], index_type=IndexType.HASH),
                )
            except ResponseError as e:
                if "already exists" not in str(e).lower():
                    raise
        self._ready = True

    async def search(self, partition: str, vector: np.ndarray) -> tuple[float, dict] | None:
        from redis.commands.search.query import Query

        await self._ensure()
        # partition is hex-only, so it needs no TAG escaping.
        q = (Query(f"(@partition:{{{partition}}})=>[KNN 1 @embedding $vec AS dist]")
             .sort_by("dist").return_fields("dist", "payload").paging(0, 1).dialect(2))
        res = await self.redis.ft(self.index).search(
            q, query_params={"vec": np.asarray(vector, dtype=np.float32).tobytes()})
        if not res.docs:
            return None
        doc = res.docs[0]
        dist = doc.dist.decode() if isinstance(doc.dist, bytes) else doc.dist
        return 1.0 - float(dist), json.loads(doc.payload)

    async def add(self, partition: str, vector: np.ndarray, payload: dict, ttl_s: int) -> None:
        await self._ensure()
        key = self.key_prefix + new_id()
        async with self.redis.pipeline(transaction=True) as p:
            p.hset(key, mapping={
                "partition": partition,
                "payload": json.dumps(payload),
                "embedding": np.asarray(vector, dtype=np.float32).tobytes(),
            })
            p.expire(key, ttl_s)  # expired hashes drop out of the index automatically
            await p.execute()


class VectorSetIndex:
    """Per-partition Redis Vector Set.

    key  {prefix}:semvs:{model}-{dim}:{partition}  vectors (NOQUANT), attribute {"exp": unix_ts}
    key  {prefix}:semvs-exp:...:{partition}       sorted set element→exp, for cleanup and caps
    key  {prefix}:sempl:{element}                 payload JSON, with the same TTL

    VSIM reports (1 + cosine) / 2; we convert it back to cosine. Expiry is enforced twice:
    a FILTER on .exp inside the query, and the payload key's own TTL.
    """

    def __init__(self, redis: Redis, dim: int, model_name: str, prefix: str = "nami",
                 max_entries: int = 500, clock: Callable[[], float] = time.time):
        slug = re.sub(r"[^a-z0-9]+", "-", model_name.lower()).strip("-")
        self.redis, self.dim, self.prefix = redis, dim, prefix
        self.ns = f"{slug}-{dim}"
        self.max_entries, self.clock = max_entries, clock

    def _vkey(self, partition: str) -> str:
        return f"{self.prefix}:semvs:{self.ns}:{partition}"

    def _ekey(self, partition: str) -> str:
        return f"{self.prefix}:semvs-exp:{self.ns}:{partition}"

    def _pkey(self, element: str) -> str:
        return f"{self.prefix}:sempl:{element}"

    async def search(self, partition: str, vector: np.ndarray) -> tuple[float, dict] | None:
        vkey = self._vkey(partition)
        try:
            res = await self.redis.vset().vsim(
                vkey, np.asarray(vector, dtype=np.float32).tobytes(), with_scores=True, count=1,
                filter=f".exp > {int(self.clock())}")
        except AttributeError:
            # redis-py 8.1 bug: when VSIM matches nothing (missing key, or everything filtered
            # out), Redis replies with an empty array but the WITHSCORES parser calls .items()
            # on it. An empty reply is the only way to reach this, so it's a miss.
            return None
        if not res:
            return None
        element, score = next(iter(res.items()))
        element = element.decode() if isinstance(element, bytes) else element
        raw = await self.redis.get(self._pkey(element))
        if raw is None:  # payload expired before the vector was cleaned up
            await self.redis.vset().vrem(vkey, element)
            return None
        return 2.0 * float(score) - 1.0, json.loads(raw)

    async def add(self, partition: str, vector: np.ndarray, payload: dict, ttl_s: int) -> None:
        from redis.commands.vectorset.commands import QuantizationOptions

        element, now = new_id(), self.clock()
        exp = int(now + ttl_s)
        vkey, ekey = self._vkey(partition), self._ekey(partition)
        async with self.redis.pipeline(transaction=True) as p:
            p.set(self._pkey(element), json.dumps(payload), ex=ttl_s)
            p.vset().vadd(vkey, np.asarray(vector, dtype=np.float32).tobytes(), element,
                          quantization=QuantizationOptions.NOQUANT, attributes={"exp": exp})
            p.zadd(ekey, {element: exp})
            p.expire(vkey, ttl_s)  # every entry shares one TTL, so the newest one bounds them all
            p.expire(ekey, ttl_s)
            await p.execute()
        await self._cleanup(partition, now)

    async def _cleanup(self, partition: str, now: float) -> None:
        vkey, ekey = self._vkey(partition), self._ekey(partition)
        stale = [e.decode() if isinstance(e, bytes) else e
                 for e in await self.redis.zrangebyscore(ekey, "-inf", int(now))]
        overflow = await self.redis.zcard(ekey) - len(stale) - self.max_entries
        if overflow > 0:  # evict the entries closest to expiry (i.e. the oldest)
            stale += [e.decode() if isinstance(e, bytes) else e
                      for e in await self.redis.zrange(ekey, len(stale), len(stale) + overflow - 1)]
        if not stale:
            return
        async with self.redis.pipeline(transaction=False) as p:
            for e in stale:
                p.vset().vrem(vkey, e)
                p.delete(self._pkey(e))
            p.zrem(ekey, *stale)
            await p.execute()


def normalise_prompt(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


class SemanticCache:
    def __init__(self, index: VectorIndex, embedder: Embedder, threshold: float, ttl_s: int):
        self.index, self.embedder = index, embedder
        self.threshold, self.ttl_s = threshold, ttl_s

    async def _vec(self, text: str) -> np.ndarray:
        return (await self.embedder.embed([normalise_prompt(text)]))[0]

    async def get(self, partition: str, text: str, threshold: float | None = None) -> SemanticHit | None:
        try:
            found = await self.index.search(partition, await self._vec(text))
        except RedisError as e:
            log.warning("semantic cache lookup failed (%s); miss", e)
            return None
        if found is None:
            return None
        sim, payload = found
        if sim < (threshold if threshold is not None else self.threshold):
            return None
        return SemanticHit(payload["response"], sim, payload["text"])

    async def put(self, partition: str, text: str, response: Any) -> None:
        try:
            await self.index.add(partition, await self._vec(text),
                                 {"text": normalise_prompt(text), "response": response}, self.ttl_s)
        except RedisError as e:
            log.warning("semantic cache write failed (%s)", e)
