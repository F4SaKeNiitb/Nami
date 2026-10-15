"""SQLite engine with the pragmas the design relies on (WAL, busy timeout, foreign keys).

The app uses a single connection (pool_size=1). SQLite allows one writer at a time anyway.
A single connection also avoids the WAL "read then upgrade to write" SQLITE_BUSY_SNAPSHOT
failure between our own connections. Rule: never hold a session across an LLM or browser
await; open short sessions for reads and writes.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base


def _sqlite_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")  # ignored for :memory:
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA busy_timeout=5000")  # other connections (Chainlit, LangGraph, Litestream)
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


def make_engine(url: str) -> AsyncEngine:
    parsed = make_url(url)
    kwargs: dict = {}
    if parsed.get_backend_name() == "sqlite":
        if parsed.database and parsed.database != ":memory:":
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
            kwargs.update(pool_size=1, max_overflow=0, pool_timeout=15)
    engine = create_async_engine(url, **kwargs)
    if engine.dialect.name == "sqlite":
        event.listen(engine.sync_engine, "connect", _sqlite_pragmas)
    return engine


class Database:
    def __init__(self, url: str):
        self.engine = make_engine(url)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Commit on success, roll back on error."""
        async with self.sessions() as s:
            try:
                yield s
                await s.commit()
            except BaseException:
                await s.rollback()
                raise

    async def create_all(self) -> None:
        """For tests and local dev. Production uses Alembic migrations."""
        import app.db.models  # noqa: F401  (register tables)

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()
