import asyncio
from logging.config import fileConfig

from alembic import context

import app.db.models  # noqa: F401  (register tables)
from app.core.config import get_settings
from app.db.base import Base
from app.db.engine import make_engine

if context.config.config_file_name:
    fileConfig(context.config.config_file_name, disable_existing_loggers=False)
URL = get_settings().database_url


def _configure(**kw):
    # render_as_batch: SQLite can't ALTER most things, so Alembic rebuilds tables instead.
    context.configure(target_metadata=Base.metadata, render_as_batch=True, compare_type=True, **kw)


def run_offline():
    _configure(url=URL, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection):
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_online():
    engine = make_engine(URL)
    async with engine.connect() as conn:
        await conn.run_sync(_run_sync)
        await conn.commit()
    await engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    asyncio.run(run_online())
