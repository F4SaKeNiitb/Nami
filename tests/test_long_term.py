from datetime import timedelta

import pytest

from app.db.base import utcnow
from app.memory.long_term import SecretInMemoryError
from app.tenancy.service import PermissionDenied


async def test_scope_visibility(svc, firm):
    org, admin, member = firm
    ch = await svc.stm.ensure_channel(org_id=org.id, kind="slack", external_id="C-ops")
    await svc.ltm.remember(org_id=org.id, scope="user", user_id=member.id, content="Ravi is vegetarian",
                           kind="preference")
    await svc.ltm.remember(org_id=org.id, scope="org", content="Acme orders lunch from swiggy on Fridays",
                           created_by=admin.id)
    await svc.ltm.remember(org_id=org.id, scope="channel", channel_id=ch.id,
                           content="ops channel deploys go to vercel project acme-web", created_by=admin.id)
    # a personal-org memory must never surface inside the firm
    await svc.ltm.remember(org_id=member.default_org_id, scope="user", user_id=member.id,
                           content="Ravi personal note about vegetarian food")

    admin_view = {m.content for m in await svc.ltm.list_visible(org_id=org.id, user_id=admin.id)}
    member_in_channel = {m.content for m in await svc.ltm.list_visible(
        org_id=org.id, user_id=member.id, channel_id=ch.id)}
    assert admin_view == {"Acme orders lunch from swiggy on Fridays"}
    assert member_in_channel == {"Ravi is vegetarian", "Acme orders lunch from swiggy on Fridays",
                                 "ops channel deploys go to vercel project acme-web"}


async def test_recall_ranks_relevance_and_updates_access(svc, firm):
    org, _, member = firm
    for c in ["Ravi is vegetarian", "Ravi's home address is Powai", "Ravi prefers UPI payments"]:
        await svc.ltm.remember(org_id=org.id, scope="user", user_id=member.id, content=c)
    top = await svc.ltm.recall(org_id=org.id, user_id=member.id, query="is Ravi vegetarian", k=1)
    assert top[0].content == "Ravi is vegetarian"
    items = await svc.ltm.list_visible(org_id=org.id, user_id=member.id)
    assert next(i for i in items if i.content == "Ravi is vegetarian").access_count == 1


async def test_recency_breaks_ties(svc, firm):
    org, _, member = firm
    await svc.ltm.remember(org_id=org.id, scope="user", user_id=member.id, content="likes paneer tikka")
    await svc.ltm.remember(org_id=org.id, scope="user", user_id=member.id, content="likes paneer biryani")
    later = utcnow() + timedelta(days=60)
    old = await svc.ltm.recall(org_id=org.id, user_id=member.id, query="likes paneer", now=later)
    assert len(old) == 2 and all(m.score < 0.7 + 0.2 + 0.1 for m in old)


async def test_dedupe_and_secret_refusal(svc, firm):
    org, _, member = firm
    a = await svc.ltm.remember(org_id=org.id, scope="user", user_id=member.id, content="Ravi is vegetarian")
    b = await svc.ltm.remember(org_id=org.id, scope="user", user_id=member.id, content="ravi is  vegetarian",
                               importance=0.9)
    assert a.id == b.id and b.importance == 0.9
    with pytest.raises(SecretInMemoryError):
        await svc.ltm.remember(org_id=org.id, scope="user", user_id=member.id,
                               content="my swiggy password is hunter2")
    with pytest.raises(SecretInMemoryError):
        await svc.ltm.remember(org_id=org.id, scope="user", user_id=member.id,
                               content="card 4111 1111 1111 1111")


async def test_forget_permissions(svc, firm):
    org, admin, member = firm
    mine = await svc.ltm.remember(org_id=org.id, scope="user", user_id=member.id, content="private")
    shared = await svc.ltm.remember(org_id=org.id, scope="org", content="firm fact", created_by=admin.id)
    with pytest.raises(PermissionDenied):
        await svc.ltm.forget(memory_id=mine.id, actor_id=admin.id)  # admins can't read/delete personal memory
    with pytest.raises(PermissionDenied):
        await svc.ltm.forget(memory_id=shared.id, actor_id=member.id)
    await svc.ltm.forget(memory_id=mine.id, actor_id=member.id)
    await svc.ltm.forget(memory_id=shared.id, actor_id=admin.id)
    assert await svc.ltm.list_visible(org_id=org.id, user_id=member.id) == []


async def test_expired_memories_hidden(svc, firm):
    org, _, member = firm
    m = await svc.ltm.remember(org_id=org.id, scope="user", user_id=member.id, content="temp", ttl_days=1)
    async with svc.db.session() as s:
        from app.db.models import MemoryItem
        (await s.get(MemoryItem, m.id)).expires_at = utcnow() - timedelta(seconds=1)
    assert await svc.ltm.list_visible(org_id=org.id, user_id=member.id) == []
