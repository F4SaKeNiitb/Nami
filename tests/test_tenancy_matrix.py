"""Every org-owned table x every role, through the real services. Written, not run (test pause).

Two parts: (1) a foreign org sees nothing in ANY org-owned table (so a new table cannot be forgotten: the table list is
checked against the registry); (2) what each role may do to the firm's data, through the services that guard it.
"""

import pytest
from sqlalchemy import func, select

from app.db.models import Task, Thread
from app.db.scoped import ScopedDatabase, org_owned_models
from app.security.authz import Resource
from app.skills.schemas import SkillContent
from app.skills.service import SkillError, SkillService
from app.skills.signing import Signer
from app.tenancy.access import Access
from app.tenancy.service import PermissionDenied
from cryptography.hazmat.primitives.asymmetric import ec

ROLES = ("owner", "admin", "member", "viewer")
KEY = ec.generate_private_key(ec.SECP256R1())


@pytest.fixture
async def firm(svc):
    async def mk(n):
        return await svc.tenancy.ensure_user(google_sub=f"g-{n}", email=f"{n}@x.com", name=n)
    owner, author, foreign = await mk("owner"), await mk("author"), await mk("foreign")
    org = await svc.tenancy.create_org(name="Acme", owner_id=owner.id)
    await svc.tenancy.add_member(org_id=org.id, user_id=author.id, role="member", actor_id=owner.id)
    users = {"owner": owner}
    for role in ("admin", "member", "viewer"):
        u = await mk(f"r-{role}")
        await svc.tenancy.add_member(org_id=org.id, user_id=u.id, role=role, actor_id=owner.id)
        users[role] = u

    ch = await svc.stm.ensure_channel(org_id=org.id, kind="slack", external_id="C1", name="ops")
    th = await svc.stm.open_thread(channel_id=ch.id, org_id=org.id, user_id=author.id, thread_id="th-1")
    async with svc.db.session() as s:
        t = Task(org_id=org.id, user_id=author.id, request_text="author's task")
        s.add(t)
        await s.flush()
        task_id = t.id
    access = Access(svc.db, svc.authz)
    await access.add_channel_member(org_id=org.id, channel_id=ch.id, user_id=users["member"].id, actor_id=owner.id)
    await svc.authz.check(await svc.authz.principal(author.id, org.id), "start_task", Resource("task", org.id, author.id))   # an authz_log row
    mem_org = await svc.ltm.remember(org_id=org.id, scope="org", content="firm policy: invoices need two approvals", created_by=owner.id)
    mem_user = await svc.ltm.remember(org_id=org.id, scope="user", user_id=author.id, content="author likes window seats")
    rule = await svc.rules.add_rule(actor_id=owner.id, org_id=org.id, kind="instruction", key="tone", value="be brief")
    skills = SkillService(svc.db, Signer(KEY))
    sk = await skills.propose(org_id=org.id, user_id=author.id, content=SkillContent(
        site="acme.com", intent="reorder", steps=["Open Orders", "Click Reorder"], pitfalls=[]),
        observed_domains=["acme.com"], plan_domains=["acme.com"], scope="org")
    return type("F", (), dict(svc=svc, org=org, owner=owner, author=author, foreign=foreign, users=users, ch=ch, th=th,
                              task_id=task_id, access=access, mem_org=mem_org, mem_user=mem_user, rule=rule,
                              skills=skills, skill=sk))


# ─────────── part 1: the foreign org sees nothing, in every org-owned table ───────────

async def test_every_org_owned_table_is_empty_for_a_foreign_org_and_populated_for_the_owner_org(firm):
    seen = set()
    for model in org_owned_models():
        async with ScopedDatabase(firm.svc.db, firm.org.id).session() as s:
            mine = await s.scalar(select(func.count()).select_from(model).where(model.org_id == firm.org.id))
        async with ScopedDatabase(firm.svc.db, firm.foreign.default_org_id).session() as s:      # asks for the firm's rows by name
            theirs = await s.scalar(select(func.count()).select_from(model).where(model.org_id == firm.org.id))
        assert mine > 0, f"fixture has no row in {model.__tablename__}: add one so the table stays covered"
        assert theirs == 0, model.__tablename__
        seen.add(model.__tablename__)
    assert seen == {"authz_log", "channel_members", "channels", "memberships", "memory_items", "rules", "skills", "tasks", "threads"}


async def test_the_foreign_orgs_own_rows_never_include_the_firms(firm):
    async with ScopedDatabase(firm.svc.db, firm.foreign.default_org_id).session() as s:
        assert (await s.scalars(select(Task))).all() == []
        assert (await s.scalars(select(Thread).where(Thread.id == firm.th.id))).all() == []


# ─────────── part 2: role x operation, through the real services ───────────

@pytest.mark.parametrize("role", ROLES)
async def test_tasks_are_private_to_their_owner_for_every_role(firm, role):
    async with firm.svc.db.session() as s:
        t = await s.get(Task, firm.task_id)
    assert not await firm.access.can_read_task(firm.users[role].id, t)
    assert await firm.access.can_read_task(firm.author.id, t)


@pytest.mark.parametrize("role", ROLES)
async def test_a_private_thread_is_hidden_from_every_role(firm, role):
    async with firm.svc.db.session() as s:
        t = await s.get(Thread, firm.th.id)
    assert not await firm.access.can_read_thread(firm.users[role].id, t)


@pytest.mark.parametrize("role,expected", [("owner", False), ("admin", False), ("member", True), ("viewer", False)])
async def test_a_channel_thread_follows_channel_membership_not_role(firm, role, expected):
    async with firm.svc.db.session() as s:
        t = await s.get(Thread, firm.th.id)
        t.visibility = "channel"
    async with firm.svc.db.session() as s:
        t = await s.get(Thread, firm.th.id)
    assert await firm.access.can_read_thread(firm.users[role].id, t) is expected         # only "member" was added to the channel


@pytest.mark.parametrize("role", ROLES)
async def test_memory_firm_memory_is_visible_to_all_roles_personal_memory_to_none(firm, role):
    u = firm.users[role]
    seen = {m.id for m in await firm.svc.ltm.list_visible(org_id=firm.org.id, user_id=u.id)}
    assert firm.mem_org.id in seen and firm.mem_user.id not in seen


@pytest.mark.parametrize("role,can", [("owner", True), ("admin", True), ("member", False), ("viewer", False)])
async def test_deleting_firm_memory_needs_an_admin_or_its_author(firm, role, can):
    u = firm.users[role]
    if can:
        await firm.svc.ltm.forget(memory_id=firm.mem_org.id, actor_id=u.id)
    else:
        with pytest.raises(PermissionDenied):
            await firm.svc.ltm.forget(memory_id=firm.mem_org.id, actor_id=u.id)


@pytest.mark.parametrize("role,firm_wide,own", [("owner", True, True), ("admin", True, True), ("member", False, True), ("viewer", False, False)])
async def test_rules_by_role(firm, role, firm_wide, own):
    u = firm.users[role]

    async def add(user_id):
        return await firm.svc.rules.add_rule(actor_id=u.id, org_id=firm.org.id, kind="instruction", key="k", value="x", user_id=user_id)
    for target, ok in ((None, firm_wide), (u.id, own)):
        if ok:
            await add(target)
        else:
            with pytest.raises(PermissionDenied):
                await add(target)


@pytest.mark.parametrize("role", ROLES)
async def test_rules_resolve_only_for_members_of_the_org(firm, role):
    eff = await firm.svc.rules.resolve(org_id=firm.org.id, user_id=firm.users[role].id)
    assert eff.org_id == firm.org.id
    with pytest.raises(PermissionDenied):
        await firm.svc.rules.resolve(org_id=firm.org.id, user_id=firm.foreign.id)


@pytest.mark.parametrize("role,can", [("owner", True), ("admin", True), ("member", False), ("viewer", False)])
async def test_approving_an_org_skill_needs_an_admin(firm, role, can):
    u = firm.users[role]
    if can:
        assert (await firm.skills.approve(firm.skill.id, actor_id=u.id, org_id=firm.org.id)).status == "approved"
    else:
        with pytest.raises(SkillError):
            await firm.skills.approve(firm.skill.id, actor_id=u.id, org_id=firm.org.id)


@pytest.mark.parametrize("role", ROLES)
async def test_a_foreign_org_cannot_even_find_the_skill(firm, role):
    with pytest.raises(SkillError):
        await firm.skills.get(firm.skill.id, firm.foreign.default_org_id)


@pytest.mark.parametrize("role,can", [("owner", True), ("admin", True), ("member", False), ("viewer", False)])
async def test_membership_and_channel_membership_are_admin_only(firm, role, can):
    u = firm.users[role]
    target = await firm.svc.tenancy.ensure_user(google_sub="g-new", email="new@x.com", name="new")
    for call in (
        lambda: firm.svc.tenancy.add_member(org_id=firm.org.id, user_id=target.id, role="member", actor_id=u.id),
    ):
        if can:
            await call()
        else:
            with pytest.raises(PermissionDenied):
                await call()
    if can:
        await firm.access.add_channel_member(org_id=firm.org.id, channel_id=firm.ch.id, user_id=target.id, actor_id=u.id)
    else:
        with pytest.raises(PermissionDenied):
            await firm.access.add_channel_member(org_id=firm.org.id, channel_id=firm.ch.id, user_id=target.id, actor_id=u.id)


@pytest.mark.parametrize("role,can", [("owner", True), ("admin", True), ("member", True), ("viewer", False)])
async def test_saving_a_credential_by_role(firm, role, can):
    from app.db.models import Membership
    u = firm.users[role]
    async with firm.svc.db.session() as s:       # credentials are judged in the user's own default org
        m = await s.scalar(select(Membership).where(Membership.org_id == u.default_org_id, Membership.user_id == u.id))
        m.role = role
    assert (await firm.access.can_create_credential(u.id)).allow is can
