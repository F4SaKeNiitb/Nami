"""channel_members: who may read a thread that is shared with its channel. Written, not run (test pause)."""

import pytest
from sqlalchemy import select

from app.db.models import ChannelMember, Task, Thread
from app.db.scoped import CrossOrgWrite, ScopedDatabase
from app.tenancy.access import Access
from app.tenancy.service import PermissionDenied


async def user(svc, n):
    return await svc.tenancy.ensure_user(google_sub=f"g-{n}", email=f"{n}@x.com", name=n)


@pytest.fixture
async def firm(svc):
    owner, author, peer, outsider_member, viewer, stranger = [await user(svc, n) for n in
                                                              ("owner", "author", "peer", "other", "viewer", "stranger")]
    org = await svc.tenancy.create_org(name="Acme", owner_id=owner.id)
    for u, role in ((author, "member"), (peer, "member"), (outsider_member, "member"), (viewer, "viewer")):
        await svc.tenancy.add_member(org_id=org.id, user_id=u.id, role=role, actor_id=owner.id)
    ch = await svc.stm.ensure_channel(org_id=org.id, kind="slack", external_id="C1", name="ops")
    access = Access(svc.db, svc.authz)

    async def thread(visibility, tid):
        t = await svc.stm.open_thread(channel_id=ch.id, org_id=org.id, user_id=author.id, thread_id=tid)
        async with svc.db.session() as s:
            (await s.get(Thread, t.id)).visibility = visibility
        async with svc.db.session() as s:
            return await s.get(Thread, t.id)

    class F: ...
    f = F()
    f.svc, f.org, f.ch, f.access, f.thread = svc, org, ch, access, thread
    f.owner, f.author, f.peer, f.other, f.viewer, f.stranger = owner, author, peer, outsider_member, viewer, stranger
    return f


async def test_a_private_thread_is_the_authors_alone_even_for_an_admin(firm):
    t = await firm.thread("private", "t-private")
    assert await firm.access.can_read_thread(firm.author.id, t)
    assert not await firm.access.can_read_thread(firm.owner.id, t)                 # admins included, by design
    assert not await firm.access.can_read_thread(firm.peer.id, t)


async def test_a_channel_thread_is_readable_by_channel_members_only(firm):
    t = await firm.thread("channel", "t-channel")
    await firm.access.add_channel_member(org_id=firm.org.id, channel_id=firm.ch.id, user_id=firm.peer.id, actor_id=firm.owner.id)
    assert await firm.access.can_read_thread(firm.peer.id, t)
    assert await firm.access.can_read_thread(firm.author.id, t)                     # the author needs no membership row
    assert not await firm.access.can_read_thread(firm.other.id, t)                  # same org, not in the channel
    assert not await firm.access.can_read_thread(firm.owner.id, t)                  # an admin is not a channel member either
    assert not await firm.access.can_read_thread(firm.stranger.id, t)               # not in the org at all


async def test_removing_a_channel_member_takes_the_access_away(firm):
    t = await firm.thread("channel", "t-channel2")
    await firm.access.add_channel_member(org_id=firm.org.id, channel_id=firm.ch.id, user_id=firm.peer.id, actor_id=firm.owner.id)
    assert await firm.access.can_read_thread(firm.peer.id, t)
    await firm.access.remove_channel_member(org_id=firm.org.id, channel_id=firm.ch.id, user_id=firm.peer.id, actor_id=firm.owner.id)
    assert not await firm.access.can_read_thread(firm.peer.id, t)


async def test_an_org_thread_is_readable_by_every_member_but_not_by_strangers(firm):
    t = await firm.thread("org", "t-org")
    for u in (firm.peer, firm.other, firm.viewer, firm.owner):
        assert await firm.access.can_read_thread(u.id, t)
    assert not await firm.access.can_read_thread(firm.stranger.id, t)


async def test_only_admins_manage_channel_members(firm):
    for actor in (firm.author, firm.viewer, firm.stranger):
        with pytest.raises(PermissionDenied):
            await firm.access.add_channel_member(org_id=firm.org.id, channel_id=firm.ch.id, user_id=firm.peer.id, actor_id=actor.id)
    async with firm.svc.db.session() as s:
        assert (await s.scalars(select(ChannelMember))).all() == []


async def test_a_non_member_of_the_org_cannot_be_added_to_a_channel(firm):
    with pytest.raises(PermissionDenied):
        await firm.access.add_channel_member(org_id=firm.org.id, channel_id=firm.ch.id, user_id=firm.stranger.id, actor_id=firm.owner.id)


async def test_adding_twice_keeps_one_row(firm):
    for _ in range(2):
        await firm.access.add_channel_member(org_id=firm.org.id, channel_id=firm.ch.id, user_id=firm.peer.id, actor_id=firm.owner.id)
    async with firm.svc.db.session() as s:
        assert len((await s.scalars(select(ChannelMember))).all()) == 1


async def test_channel_members_are_scoped_to_their_org(firm):
    await firm.access.add_channel_member(org_id=firm.org.id, channel_id=firm.ch.id, user_id=firm.peer.id, actor_id=firm.owner.id)
    other_org = firm.stranger.default_org_id
    async with ScopedDatabase(firm.svc.db, other_org).session() as s:
        assert (await s.scalars(select(ChannelMember))).all() == []              # invisible from another org
    with pytest.raises(CrossOrgWrite):
        async with ScopedDatabase(firm.svc.db, other_org).session() as s:
            s.add(ChannelMember(org_id=firm.org.id, channel_id=firm.ch.id, user_id=firm.other.id))


async def test_a_task_is_readable_only_by_its_owner_even_for_an_admin(firm):
    async with firm.svc.db.session() as s:
        t = Task(org_id=firm.org.id, user_id=firm.author.id, request_text="x")
        s.add(t)
        await s.flush()
        tid = t.id
    async with firm.svc.db.session() as s:
        t = await s.get(Task, tid)
    assert await firm.access.can_read_task(firm.author.id, t)
    assert not await firm.access.can_read_task(firm.owner.id, t)
    assert not await firm.access.can_read_task(firm.peer.id, t)


async def test_an_owner_without_any_membership_row_keeps_working(svc):
    """Rows made before memberships existed: the owner can still read their own task (today's behaviour)."""
    u = await user(svc, "legacy")
    org = await svc.tenancy.create_org(name="Solo", owner_id=(await user(svc, "someone")).id)
    async with svc.db.session() as s:
        t = Task(org_id=org.id, user_id=u.id, request_text="mine")
        s.add(t)
        await s.flush()
        tid = t.id
    async with svc.db.session() as s:
        t = await s.get(Task, tid)
    acc = Access(svc.db, svc.authz)
    assert await acc.can_read_task(u.id, t)
    assert not await acc.can_read_task((await user(svc, "nosy")).id, t)
