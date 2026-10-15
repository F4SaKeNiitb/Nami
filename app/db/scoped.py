"""Org-scoped database access: every query on an org-owned table is filtered to one organisation, automatically.

Services that read or write firm data should open `ScopedDatabase(db, org_id).session()` instead of a raw session, so a
forgotten `WHERE org_id = ...` cannot leak another company's rows. How it works:
  * a `do_orm_execute` hook attaches `with_loader_criteria(Model, Model.org_id == org)` to every SELECT, UPDATE and DELETE,
    including joins, subqueries and aliases, for every mapped model that has an `org_id` column;
  * a `before_flush` hook refuses to insert or modify a row that belongs to a different org.
Raw SQL text() is NOT scoped; tests/test_scoped_session.py statically keeps raw-session use on an allow-list.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import with_loader_criteria

from app.db.base import Base
from app.db.engine import Database


class CrossOrgWrite(Exception):
    pass


def org_owned_models() -> list[type]:
    import app.db.models  # noqa: F401  (register tables)
    return [m.class_ for m in Base.registry.mappers if "org_id" in m.columns]


class ScopedDatabase:
    def __init__(self, db: Database, org_id: str):
        if not org_id:
            raise ValueError("an org id is required")
        self.db, self.org_id = db, org_id

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        org_id, models = self.org_id, org_owned_models()
        async with self.db.sessions() as s:
            @event.listens_for(s.sync_session, "do_orm_execute")
            def _scope(state):
                if state.is_select or state.is_update or state.is_delete:
                    # The org must be a *closure variable*: SQLAlchemy caches lambdas and tracks closure values as bound
                    # parameters. A default-argument capture is not tracked, so the first org would stick for every later query.
                    state.statement = state.statement.options(*[
                        with_loader_criteria(m, lambda cls: cls.org_id == org_id, include_aliases=True) for m in models])

            @event.listens_for(s.sync_session, "before_flush")
            def _guard(session, _ctx, _instances):
                for obj in [*session.new, *session.dirty]:
                    if type(obj) in models and getattr(obj, "org_id", org_id) not in (org_id, None):
                        raise CrossOrgWrite(f"{type(obj).__name__} belongs to another organisation")
            try:
                yield s
                await s.commit()
            except BaseException:
                await s.rollback()
                raise
