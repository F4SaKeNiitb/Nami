from sqlalchemy import text


async def test_sqlite_pragmas(svc):
    async with svc.db.session() as s:
        assert (await s.execute(text("PRAGMA journal_mode"))).scalar() == "wal"
        assert (await s.execute(text("PRAGMA foreign_keys"))).scalar() == 1
        assert (await s.execute(text("PRAGMA busy_timeout"))).scalar() == 5000


async def test_first_login_creates_personal_org(svc):
    u = await svc.tenancy.ensure_user(google_sub="g1", email="a@b.c", name="A")
    again = await svc.tenancy.ensure_user(google_sub="g1", email="a@b.c", name="A")
    assert u.id == again.id and u.default_org_id
