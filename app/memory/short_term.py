"""Short-term memory: what the model sees about the *current* conversation.

Per thread, the context is a rolling summary of older turns plus the last N turns verbatim.
Per channel, it's the recent messages across all threads in that channel (useful for
shared surfaces like a Slack channel).

SQLite is the source of truth. Redis holds a hot copy of the recent window:
  - appends use RPUSHX, so a missing list is never half-filled; the next read rebuilds it
    in full from SQLite
  - every turn carries its seq, and reads sort by it, so concurrent appends can't reorder history
  - any Redis failure just means "read from SQLite"
"""

import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select

from app.core.config import Settings
from app.db.base import utcnow
from app.db.engine import Database
from app.db.models import Channel, Message, Thread
from app.security.secrets import redact

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Turn:
    seq: int
    role: str
    content: str
    thread_id: str = ""


@dataclass(frozen=True)
class ThreadContext:
    summary: str
    recent: list[Turn]

    def as_messages(self) -> list[dict]:
        msgs = []
        if self.summary:
            msgs.append({"role": "user", "content": f"<conversation_summary>\n{self.summary}\n</conversation_summary>"})
        msgs += [{"role": t.role, "content": t.content} for t in self.recent if t.role in ("user", "assistant")]
        return msgs


# (previous_summary, turns_to_fold) -> new_summary. Backed by a cheap LLM in production.
Summarizer = Callable[[str, Sequence[Turn]], Awaitable[str]]


class ShortTermMemory:
    def __init__(self, db: Database, redis: Redis | None, settings: Settings, summarizer: Summarizer | None = None):
        self.db, self.redis, self.s = db, redis, settings
        self.summarizer = summarizer
        self.prefix = settings.redis_prefix

    def _tkey(self, thread_id: str) -> str:
        return f"{self.prefix}:stm:t:{thread_id}"

    def _ckey(self, channel_id: str) -> str:
        return f"{self.prefix}:stm:c:{channel_id}"

    # ───────── threads ─────────
    async def open_thread(self, *, channel_id: str, org_id: str, user_id: str | None,
                          thread_id: str | None = None, title: str | None = None) -> Thread:
        async with self.db.session() as s:
            if thread_id and (existing := await s.get(Thread, thread_id)):
                return existing
            t = Thread(channel_id=channel_id, org_id=org_id, user_id=user_id, title=title)
            if thread_id:
                t.id = thread_id
            s.add(t)
            return t

    async def ensure_channel(self, *, org_id: str, kind: str, external_id: str, name: str | None = None) -> Channel:
        async with self.db.session() as s:
            ch = await s.scalar(select(Channel).where(
                Channel.org_id == org_id, Channel.kind == kind, Channel.external_id == external_id))
            if ch is None:
                ch = Channel(org_id=org_id, kind=kind, external_id=external_id, name=name)
                s.add(ch)
            return ch

    async def append(self, thread_id: str, role: str, content: str, author_user_id: str | None = None) -> Turn:
        content = redact(content)  # defence in depth: the chat prefilter should already have caught secrets
        async with self.db.session() as s:
            thread = await s.get(Thread, thread_id, with_for_update=True)
            if thread is None:
                raise LookupError(f"thread {thread_id}")
            thread.last_seq += 1
            thread.updated_at = utcnow()
            s.add(Message(thread_id=thread_id, seq=thread.last_seq, role=role,
                          author_user_id=author_user_id, content=content))
            turn = Turn(thread.last_seq, role, content, thread_id)
            channel_id = thread.channel_id
            needs_fold = thread.last_seq - thread.summary_upto_seq > self.s.stm_window_turns + self.s.stm_fold_batch

        await self._push(self._tkey(thread_id), turn, self.s.stm_window_turns)
        await self._push(self._ckey(channel_id), turn, self.s.stm_channel_window)
        if needs_fold and self.summarizer:
            await self._fold(thread_id)
        return turn

    async def context(self, thread_id: str) -> ThreadContext:
        async with self.db.session() as s:
            thread = await s.get(Thread, thread_id)
            if thread is None:
                raise LookupError(f"thread {thread_id}")
            summary, upto = thread.summary, thread.summary_upto_seq

        recent = await self._read(self._tkey(thread_id))
        if recent is None:
            recent = await self._rebuild_thread(thread_id)
        # Never show a turn twice: anything already folded into the summary is dropped.
        return ThreadContext(summary, sorted((t for t in recent if t.seq > upto), key=lambda t: t.seq))

    # ───────── channels ─────────
    async def channel_recent(self, channel_id: str) -> list[Turn]:
        cached = await self._read(self._ckey(channel_id))
        if cached is not None:
            return cached
        async with self.db.session() as s:
            rows = (await s.execute(
                select(Message.seq, Message.role, Message.content, Message.thread_id, Message.created_at)
                .join(Thread, Thread.id == Message.thread_id)
                .where(Thread.channel_id == channel_id)
                .order_by(Message.created_at.desc(), Message.seq.desc())
                .limit(self.s.stm_channel_window))).all()
        turns = [Turn(r.seq, r.role, r.content, r.thread_id) for r in reversed(rows)]
        await self._fill(self._ckey(channel_id), turns, sort=False)
        return turns

    # ───────── summary folding ─────────
    async def _fold(self, thread_id: str) -> None:
        async with self.db.session() as s:
            thread = await s.get(Thread, thread_id)
            prev_summary, upto = thread.summary, thread.summary_upto_seq
            fold_to = thread.last_seq - self.s.stm_window_turns
            rows = (await s.scalars(select(Message).where(
                Message.thread_id == thread_id, Message.seq > upto, Message.seq <= fold_to
            ).order_by(Message.seq))).all()
            turns = [Turn(m.seq, m.role, m.content, thread_id) for m in rows]
        if not turns:
            return
        new_summary = await self.summarizer(prev_summary, turns)  # no DB session held during the LLM call
        async with self.db.session() as s:
            thread = await s.get(Thread, thread_id)
            if thread.summary_upto_seq != upto:  # someone else folded meanwhile; theirs wins
                return
            thread.summary = redact(new_summary)
            thread.summary_upto_seq = turns[-1].seq

    # ───────── redis helpers ─────────
    async def _push(self, key: str, turn: Turn, window: int) -> None:
        if self.redis is None:
            return
        try:
            async with self.redis.pipeline(transaction=True) as p:
                p.rpushx(key, json.dumps(asdict(turn)))
                p.ltrim(key, -window, -1)
                p.expire(key, self.s.stm_redis_ttl_s)
                await p.execute()
        except RedisError as e:
            log.warning("stm push failed (%s); dropping hot copy", e)
            await self._drop(key)

    async def _read(self, key: str) -> list[Turn] | None:
        if self.redis is None:
            return None
        try:
            raw = await self.redis.lrange(key, 0, -1)
        except RedisError as e:
            log.warning("stm read failed (%s)", e)
            return None
        if not raw:
            return None
        return [Turn(**json.loads(x)) for x in raw]

    async def _fill(self, key: str, turns: list[Turn], sort: bool = True) -> None:
        if self.redis is None or not turns:
            return
        if sort:
            turns = sorted(turns, key=lambda t: t.seq)
        try:
            async with self.redis.pipeline(transaction=True) as p:
                p.delete(key)
                p.rpush(key, *[json.dumps(asdict(t)) for t in turns])
                p.expire(key, self.s.stm_redis_ttl_s)
                await p.execute()
        except RedisError as e:
            log.warning("stm fill failed (%s)", e)

    async def _drop(self, key: str) -> None:
        try:
            await self.redis.delete(key)
        except RedisError:
            pass

    async def _rebuild_thread(self, thread_id: str) -> list[Turn]:
        async with self.db.session() as s:
            rows = (await s.scalars(select(Message).where(Message.thread_id == thread_id)
                                    .order_by(Message.seq.desc()).limit(self.s.stm_window_turns))).all()
        turns = sorted((Turn(m.seq, m.role, m.content, thread_id) for m in rows), key=lambda t: t.seq)
        await self._fill(self._tkey(thread_id), turns)
        return turns
