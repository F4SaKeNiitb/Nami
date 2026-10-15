async def _thread(svc, org, user, kind="web", ext="web"):
    ch = await svc.stm.ensure_channel(org_id=org.id, kind=kind, external_id=ext)
    t = await svc.stm.open_thread(channel_id=ch.id, org_id=org.id, user_id=user.id)
    return ch, t


async def test_window_and_redis_rebuild(svc, firm, redis):
    org, admin, _ = firm
    _, t = await _thread(svc, org, admin)
    for i in range(6):
        await svc.stm.append(t.id, "user" if i % 2 == 0 else "assistant", f"turn {i + 1}")
    ctx = await svc.stm.context(t.id)  # first read fills Redis from SQLite
    assert [x.seq for x in ctx.recent] == [3, 4, 5, 6]

    await redis.flushall()  # lose the hot copy entirely
    await svc.stm.append(t.id, "user", "turn 7")  # RPUSHX must not create a 1-item list
    ctx = await svc.stm.context(t.id)
    assert [x.seq for x in ctx.recent] == [4, 5, 6, 7]


async def test_secrets_are_redacted_before_storage(svc, firm):
    org, admin, _ = firm
    _, t = await _thread(svc, org, admin)
    await svc.stm.append(t.id, "user", "my key is sk-or-v1-abcdefghijklmnopqrstuvwxyz123456")
    ctx = await svc.stm.context(t.id)
    assert "sk-or-v1" not in ctx.recent[0].content and "[REDACTED:" in ctx.recent[0].content


async def test_summary_folding(make_services):
    calls = []

    async def summarizer(prev, turns):
        calls.append([t.seq for t in turns])
        return (prev + " | " if prev else "") + ",".join(t.content for t in turns)

    svc = await make_services(summarizer=summarizer)
    u = await svc.tenancy.ensure_user(google_sub="s", email="s@s.s")
    from app.db.models import Org
    async with svc.db.session() as s:
        org = await s.get(Org, u.default_org_id)
    _, t = await _thread(svc, org, u)
    for i in range(1, 8):  # window 4 + batch 2 → fold triggers at seq 7
        await svc.stm.append(t.id, "user", f"m{i}")
    assert calls == [[1, 2, 3]]
    ctx = await svc.stm.context(t.id)
    assert ctx.summary == "m1,m2,m3"
    assert [x.seq for x in ctx.recent] == [4, 5, 6, 7]  # no overlap with the summary
    assert ctx.as_messages()[0]["content"].startswith("<conversation_summary>")


async def test_channel_recent_spans_threads(svc, firm):
    org, admin, member = firm
    ch, t1 = await _thread(svc, org, admin, kind="slack", ext="C123")
    t2 = await svc.stm.open_thread(channel_id=ch.id, org_id=org.id, user_id=member.id)
    await svc.stm.append(t1.id, "user", "from asha")
    await svc.stm.append(t2.id, "user", "from ravi")
    recent = await svc.stm.channel_recent(ch.id)
    assert {x.content for x in recent} == {"from asha", "from ravi"}


async def test_works_without_redis(make_services):
    svc = await make_services(use_redis=False)
    u = await svc.tenancy.ensure_user(google_sub="n", email="n@n.n")
    from app.db.models import Org
    async with svc.db.session() as s:
        org = await s.get(Org, u.default_org_id)
    _, t = await _thread(svc, org, u)
    await svc.stm.append(t.id, "user", "hello")
    assert [x.content for x in (await svc.stm.context(t.id)).recent] == ["hello"]
