"""Long-term memory: durable facts and preferences that outlive a single chat.

Visibility is always inside one firm (org):
  user scope     only that user
  org scope      everyone in the firm
  channel scope  anyone in that channel

So a preference saved in a personal org never shows up in a firm's context.

Ranking follows the Generative Agents recipe (Park et al., 2023):
  score = 0.7·similarity + 0.2·recency + 0.1·importance
  recency = 0.5^(age_days / half_life)

Retrieval is brute-force cosine in numpy over the caller's visible rows. A user's memory
set is small (hundreds of rows), so this is fast. Move to sqlite-vec once a scope passes
~50k rows.
"""

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import and_, delete, or_, select

from app.core.config import Settings
from app.db.base import utcnow
from app.db.engine import Database
from app.db.scoped import ScopedDatabase
from app.db.models import MemoryItem
from app.memory.embeddings import Embedder, from_blob, to_blob
from app.security.secrets import contains_secret
from app.tenancy.service import PermissionDenied, role_in

log = logging.getLogger(__name__)


class SecretInMemoryError(ValueError):
    """Raised instead of storing anything secret-shaped. Secrets belong in the vault."""


@dataclass(frozen=True)
class RecalledMemory:
    id: str
    content: str
    kind: str
    scope: str
    score: float
    similarity: float


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


class LongTermMemory:
    def __init__(self, db: Database, embedder: Embedder, settings: Settings):
        self.db, self.embedder, self.s = db, embedder, settings

    async def remember(self, *, org_id: str, scope: str, content: str, kind: str = "fact",
                       user_id: str | None = None, channel_id: str | None = None,
                       importance: float = 0.5, source_thread_id: str | None = None,
                       created_by: str | None = None, ttl_days: int | None = None) -> MemoryItem:
        content = _norm(content)
        if not content:
            raise ValueError("empty memory")
        if contains_secret(content):
            raise SecretInMemoryError("refusing to store a secret in memory; use the vault")
        if scope == "user" and not user_id:
            raise ValueError("user-scoped memory needs user_id")
        if scope == "channel" and not channel_id:
            raise ValueError("channel-scoped memory needs channel_id")
        h = hashlib.sha256(content.lower().encode()).hexdigest()
        vec = (await self.embedder.embed([content]))[0]  # embed before opening a session

        async with self.db.session() as s:
            owner = [MemoryItem.org_id == org_id, MemoryItem.scope == scope,
                     MemoryItem.user_id.is_(user_id) if user_id is None else MemoryItem.user_id == user_id,
                     MemoryItem.channel_id.is_(None) if channel_id is None else MemoryItem.channel_id == channel_id,
                     MemoryItem.embedding_model == self.embedder.model_name]
            same_owner = list(await s.scalars(select(MemoryItem).where(*owner)))

            # Exact duplicate: reinforce. Near duplicate: the newer wording supersedes.
            match = next((m for m in same_owner if m.content_hash == h), None)
            if match is None and same_owner:
                sims = np.stack([from_blob(m.embedding) for m in same_owner]) @ vec
                i = int(np.argmax(sims))
                if sims[i] >= self.s.ltm_dedupe_similarity:
                    match = same_owner[i]
            if match is not None:
                match.content, match.content_hash, match.embedding = content, h, to_blob(vec)
                match.importance = max(match.importance, importance)
                match.updated_at = utcnow()
                return match

            item = MemoryItem(
                org_id=org_id, scope=scope, user_id=user_id, channel_id=channel_id, kind=kind,
                content=content, content_hash=h, embedding=to_blob(vec),
                embedding_model=self.embedder.model_name, embedding_dim=self.embedder.dim,
                importance=min(max(importance, 0.0), 1.0), source_thread_id=source_thread_id,
                created_by=created_by,
                expires_at=utcnow() + timedelta(days=ttl_days) if ttl_days else None)
            s.add(item)
            return item

    def _visible(self, org_id: str, user_id: str, channel_id: str | None):
        now = utcnow()
        clauses = [MemoryItem.scope == "org", and_(MemoryItem.scope == "user", MemoryItem.user_id == user_id)]
        if channel_id:
            clauses.append(and_(MemoryItem.scope == "channel", MemoryItem.channel_id == channel_id))
        return and_(MemoryItem.org_id == org_id, or_(*clauses),
                    or_(MemoryItem.expires_at.is_(None), MemoryItem.expires_at > now),
                    MemoryItem.embedding_model == self.embedder.model_name)

    async def recall(self, *, org_id: str, user_id: str, query: str, channel_id: str | None = None,
                     k: int | None = None, kinds: set[str] | None = None,
                     now: datetime | None = None) -> list[RecalledMemory]:
        qvec = (await self.embedder.embed([_norm(query)]))[0]
        async with ScopedDatabase(self.db, org_id).session() as s:
            stmt = select(MemoryItem.id, MemoryItem.content, MemoryItem.kind, MemoryItem.scope,
                          MemoryItem.embedding, MemoryItem.importance, MemoryItem.updated_at,
                          MemoryItem.last_accessed_at).where(self._visible(org_id, user_id, channel_id))
            if kinds:
                stmt = stmt.where(MemoryItem.kind.in_(kinds))
            rows = (await s.execute(stmt)).all()
        if not rows:
            return []

        now = now or utcnow()
        sims = np.stack([from_blob(r.embedding) for r in rows]) @ qvec
        half_life = self.s.ltm_recency_half_life_days
        scored = []
        for r, sim in zip(rows, sims):
            touched = max(r.updated_at, r.last_accessed_at or r.updated_at)
            recency = 0.5 ** (max((now - touched).total_seconds(), 0) / 86400 / half_life)
            score = 0.7 * float(sim) + 0.2 * recency + 0.1 * r.importance
            if score >= self.s.ltm_min_score:
                scored.append(RecalledMemory(r.id, r.content, r.kind, r.scope, score, float(sim)))
        top = sorted(scored, key=lambda m: m.score, reverse=True)[: k or self.s.ltm_top_k]

        if top:
            async with ScopedDatabase(self.db, org_id).session() as s:
                for item in await s.scalars(select(MemoryItem).where(MemoryItem.id.in_([m.id for m in top]))):
                    item.access_count += 1
                    item.last_accessed_at = now
        return top

    async def list_visible(self, *, org_id: str, user_id: str, channel_id: str | None = None) -> list[MemoryItem]:
        async with ScopedDatabase(self.db, org_id).session() as s:
            return list(await s.scalars(select(MemoryItem).where(
                self._visible(org_id, user_id, channel_id)).order_by(MemoryItem.updated_at.desc())))

    async def forget(self, *, memory_id: str, actor_id: str) -> None:
        """Hard delete. User memories: only the owner. Firm/channel memories: the author or an admin."""
        async with self.db.session() as s:
            item = await s.get(MemoryItem, memory_id)
            if item is None:
                return
            role = await role_in(s, item.org_id, actor_id)
            if item.scope == "user":
                allowed = item.user_id == actor_id
            else:
                allowed = role in ("owner", "admin") or (role is not None and item.created_by == actor_id)
            if not allowed:
                raise PermissionDenied("not allowed to delete this memory")
            await s.delete(item)

    async def forget_user(self, *, org_id: str, user_id: str) -> int:
        async with self.db.session() as s:
            res = await s.execute(delete(MemoryItem).where(
                MemoryItem.org_id == org_id, MemoryItem.scope == "user", MemoryItem.user_id == user_id))
            return res.rowcount or 0
