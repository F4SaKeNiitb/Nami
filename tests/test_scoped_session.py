"""ScopedSession: a forgotten org filter cannot leak another company's rows."""

import ast
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import aliased

from app.db.models import Channel, MemoryItem, Rule, Skill, Task, Thread
from app.db.scoped import CrossOrgWrite, ScopedDatabase, org_owned_models


@pytest.fixture
async def two_orgs(svc):
    a = await svc.tenancy.ensure_user(google_sub="ga", email="a@a.com", name="A")
    b = await svc.tenancy.ensure_user(google_sub="gb", email="b@b.com", name="B")
    async with svc.db.session() as s:
        for u in (a, b):
            s.add(Task(org_id=u.default_org_id, user_id=u.id, request_text=f"task of {u.email}"))
            s.add(Task(org_id=u.default_org_id, user_id=u.id, request_text=f"second task of {u.email}", status="done"))
            s.add(Rule(org_id=u.default_org_id, user_id=u.id, kind="policy", key="k", value=1, enforcement="soft"))
    return svc.db, a, b


async def test_every_org_owned_table_is_covered_automatically():
    names = {m.__tablename__ for m in org_owned_models()}
    assert {"tasks", "rules", "threads", "channels", "memory_items", "skills", "memberships", "authz_log"} <= names


async def test_selects_joins_subqueries_and_counts_only_see_the_scoped_org(two_orgs):
    db, a, b = two_orgs
    scoped = ScopedDatabase(db, a.default_org_id)
    async with scoped.session() as s:
        rows = (await s.scalars(select(Task))).all()
        assert {t.org_id for t in rows} == {a.default_org_id} and len(rows) == 2
        assert await s.scalar(select(func.count()).select_from(Task)) == 2
        t2 = aliased(Task)
        both = (await s.execute(select(Task.id).join(t2, Task.user_id == t2.user_id))).all()
        assert len(both) == 4                                            # 2 x 2 within one org, none from the other
        sub = select(Task.user_id).scalar_subquery()
        assert (await s.scalars(select(Rule).where(Rule.user_id.in_(sub)))).all()[0].org_id == a.default_org_id
        foreign = await s.scalar(select(Task.id).where(Task.org_id == b.default_org_id))
        assert foreign is None                                           # asking for the other org by name still finds nothing


async def test_the_same_query_under_two_orgs_never_reuses_the_first_orgs_filter(two_orgs):
    """Regression: SQLAlchemy caches the loader-criteria lambda; capturing the org wrongly made the first org stick."""
    db, a, b = two_orgs
    for _ in range(3):
        for u in (a, b, a):
            async with ScopedDatabase(db, u.default_org_id).session() as s:
                assert {t.org_id for t in (await s.scalars(select(Task))).all()} == {u.default_org_id}


async def test_get_by_id_cannot_fetch_another_orgs_row(two_orgs):
    db, a, b = two_orgs
    async with db.session() as s:
        b_task = (await s.scalars(select(Task).where(Task.org_id == b.default_org_id))).first().id
    async with ScopedDatabase(db, a.default_org_id).session() as s:
        assert await s.get(Task, b_task) is None


async def test_update_and_delete_cannot_touch_another_orgs_rows(two_orgs):
    db, a, b = two_orgs
    async with ScopedDatabase(db, a.default_org_id).session() as s:
        r1 = await s.execute(update(Task).values(status="failed"))
        r2 = await s.execute(delete(Rule))
        assert r1.rowcount == 2 and r2.rowcount == 1
    async with db.session() as s:
        statuses = {(t.org_id, t.status) for t in (await s.scalars(select(Task))).all()}
        assert (b.default_org_id, "planning") in statuses and (b.default_org_id, "done") in statuses
        assert await s.scalar(select(func.count()).select_from(Rule).where(Rule.org_id == b.default_org_id)) == 1


async def test_writing_a_row_for_another_org_is_refused(two_orgs):
    db, a, b = two_orgs
    with pytest.raises(CrossOrgWrite):
        async with ScopedDatabase(db, a.default_org_id).session() as s:
            s.add(Task(org_id=b.default_org_id, user_id=a.id, request_text="planted"))
    with pytest.raises(CrossOrgWrite):
        async with ScopedDatabase(db, a.default_org_id).session() as s:      # moving an existing row to another org
            t = (await s.scalars(select(Task))).first()
            t.org_id = b.default_org_id
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(Task).where(Task.request_text == "planted")) == 0


async def test_scope_requires_an_org(two_orgs):
    with pytest.raises(ValueError):
        ScopedDatabase(two_orgs[0], "")


async def test_a_raw_session_is_unscoped_which_is_why_it_is_allow_listed(two_orgs):
    db, *_ = two_orgs
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(Task)) == 4


# ───────────── static guard: raw sessions stay on an allow-list ─────────────

ROOT = Path(__file__).resolve().parent.parent / "app"
# Modules that predate ScopedSession and filter by user_id/org_id by hand. New firm-data code must use ScopedDatabase.
RAW_SESSION_ALLOWLIST = {
    "admin.py", "agent/graph.py", "browser/session_manager.py", "eval/harness.py", "memory/long_term.py",
    "memory/short_term.py", "rules/engine.py", "security/authz.py", "tenancy/service.py", "vault/service.py",
    "skills/service.py", "eval/run_eval.py",
    "slack/links.py",            # keyed by a Slack identity, looked up before any user or org is known; it is how that identity becomes one
    "tenancy/access.py",         # reads a row to learn its org (default org, a thread by id) before the decision can scope anything
}


def _uses_raw_session(path: Path) -> bool:
    return any(isinstance(n, ast.Attribute) and n.attr == "session" and isinstance(n.value, ast.Attribute) and n.value.attr == "db"
               for n in ast.walk(ast.parse(path.read_text())))


def test_no_new_module_opens_a_raw_session_without_being_reviewed():
    users = {str(p.relative_to(ROOT)) for p in ROOT.rglob("*.py") if _uses_raw_session(p)}
    assert users <= RAW_SESSION_ALLOWLIST, f"use ScopedDatabase instead of db.session() in: {sorted(users - RAW_SESSION_ALLOWLIST)}"


def test_allow_list_has_no_dead_entries():
    users = {str(p.relative_to(ROOT)) for p in ROOT.rglob("*.py") if _uses_raw_session(p)}
    assert RAW_SESSION_ALLOWLIST - users == set(), "remove modules that no longer use a raw session"
