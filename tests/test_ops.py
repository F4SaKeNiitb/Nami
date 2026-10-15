"""Operations: concurrent writers on SQLite (no 'database is locked') and a backup/restore drill."""

import asyncio
import os
import sqlite3
import subprocess
import sys

import pytest

from app.core.config import Settings
from app.core.container import build_services
from app.db.models import TaskEvent
from app.memory.embeddings import HashingEmbedder
from app.vault.kms import LocalKms
from app.vault.service import VaultService


@pytest.fixture
async def filedb(tmp_path):
    s = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/ops.db", redis_url=None)
    svc = build_services(s, redis=None, embedder=HashingEmbedder(), semantic_backend="memory")
    await svc.db.create_all()
    yield svc, tmp_path / "ops.db"
    await svc.db.dispose()


async def test_agent_events_vault_writes_and_a_second_connection_do_not_lock_each_other(filedb):
    svc, path = filedb
    vault = VaultService(svc.db, LocalKms())
    users = [(await svc.tenancy.ensure_user(google_sub=f"g{i}", email=f"u{i}@x.com", name="U")) for i in range(4)]
    errors: list[Exception] = []

    async def vault_work(u):
        for i in range(8):
            c = await vault.store(user_id=u.id, kind="api_key", label=f"k{i}", origins=["api.example.com"], secret={"key": f"sk-test-{i}-{'x' * 24}"[:40]})
            await vault.use(user_id=u.id, credential_id=c.id, origin="https://api.example.com", actor="t")

    async def event_work(u):
        for i in range(40):
            async with svc.db.session() as s:
                s.add(TaskEvent(task_id=f"t{u.id[:6]}", type="step", payload={"i": i}))

    async def authz_work(u):
        from app.security.authz import Principal, Resource
        for _ in range(30):
            await svc.authz.check(Principal(u.id, u.default_org_id, "owner"), "start_task", Resource("org", u.default_org_id, id=u.default_org_id))

    def other_connection():          # what Chainlit / LangGraph / Litestream do: another connection writing the same file
        con = sqlite3.connect(path, timeout=5)
        con.execute("PRAGMA busy_timeout=5000")
        for i in range(40):
            con.execute("INSERT INTO task_events (id, task_id, type, payload, ts) VALUES (?, 'ext', 'step', '{}', datetime('now'))", (f"ext{i:05d}",))
            con.commit()
        con.close()

    async def guarded(coro):
        try:
            await coro
        except Exception as e:                                   # noqa: BLE001
            errors.append(e)

    await asyncio.gather(*[guarded(f(u)) for u in users for f in (vault_work, event_work, authz_work)],
                         guarded(asyncio.to_thread(other_connection)))
    assert not errors, errors[:3]
    assert not any("locked" in str(e).lower() for e in errors)
    async with svc.db.session() as s:
        from sqlalchemy import func, select
        assert await s.scalar(select(func.count()).select_from(TaskEvent)) == 4 * 40 + 40


async def test_backup_restore_drill_vault_still_decrypts_and_schema_is_at_head(tmp_path):
    src = tmp_path / "live.db"
    s = Settings(database_url=f"sqlite+aiosqlite:///{src}", redis_url=None)
    env = {**os.environ, "PYTHONPATH": ".", "NAMI_DATABASE_URL": s.database_url}
    assert subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], capture_output=True, env=env, timeout=120).returncode == 0
    kms = LocalKms()                                              # the KEK lives outside the database, as in production
    live = build_services(s, redis=None, embedder=HashingEmbedder(), semantic_backend="memory")
    vault = VaultService(live.db, kms)
    uid = (await live.tenancy.ensure_user(google_sub="g", email="a@x.com", name="A")).id
    cred = await vault.store(user_id=uid, kind="password", label="x", origins=["acme.com"], secret={"username": "me", "password": "Tr0ub4dor&3-unique"})
    await live.db.dispose()

    restored = tmp_path / "restored.db"                           # what `litestream restore` produces: a consistent copy
    a, b = sqlite3.connect(src), sqlite3.connect(restored)
    a.backup(b); a.close(); b.close()
    r = build_services(Settings(database_url=f"sqlite+aiosqlite:///{restored}", redis_url=None), redis=None,
                       embedder=HashingEmbedder(), semantic_backend="memory")
    try:
        payload = await VaultService(r.db, kms).use(user_id=uid, credential_id=cred.id, origin="https://acme.com", actor="drill")
        assert payload["password"] == "Tr0ub4dor&3-unique"
        with pytest.raises(Exception):                            # a different KEK (lost/rotated key) cannot open the restore
            await VaultService(r.db, LocalKms()).use(user_id=uid, credential_id=cred.id, origin="https://acme.com", actor="drill")
    finally:
        await r.db.dispose()
    cur = subprocess.run([sys.executable, "-m", "alembic", "current"], capture_output=True, text=True, timeout=60,
                         env={**env, "NAMI_DATABASE_URL": f"sqlite+aiosqlite:///{restored}"})
    assert "(head)" in cur.stdout + cur.stderr


async def test_a_restored_backup_still_holds_old_wrapped_keys_which_is_why_retention_matters(tmp_path):
    """Documents a limit: crypto-shred destroys the key in the LIVE database; an older backup still has the wrapped key.
    Shredding is only complete once the replica retention window (72 h) has passed."""
    s = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/a.db", redis_url=None)
    svc = build_services(s, redis=None, embedder=HashingEmbedder(), semantic_backend="memory")
    await svc.db.create_all()
    kms = LocalKms()
    vault = VaultService(svc.db, kms)
    uid = (await svc.tenancy.ensure_user(google_sub="g", email="a@x.com", name="A")).id
    cred = await vault.store(user_id=uid, kind="api_key", label="k", origins=["api.example.com"], secret={"key": "sk-test-" + "x" * 30})
    await svc.db.engine.dispose()
    a, b = sqlite3.connect(tmp_path / "a.db"), sqlite3.connect(tmp_path / "old_backup.db")
    a.backup(b); a.close(); b.close()
    live = build_services(s, redis=None, embedder=HashingEmbedder(), semantic_backend="memory")
    await VaultService(live.db, kms).shred_user(uid)                              # shred in the LIVE database
    with pytest.raises(Exception):
        await VaultService(live.db, kms).use(user_id=uid, credential_id=cred.id, origin="https://api.example.com", actor="t")
    await live.db.dispose()
    old = build_services(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/old_backup.db", redis_url=None),
                         redis=None, embedder=HashingEmbedder(), semantic_backend="memory")
    try:
        assert (await VaultService(old.db, kms).use(user_id=uid, credential_id=cred.id, origin="https://api.example.com", actor="t"))["key"].startswith("sk-test-")
    finally:
        await old.db.dispose()
