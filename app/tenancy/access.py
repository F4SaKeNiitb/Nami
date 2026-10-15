"""Enforcement helpers: turn real rows into the (Principal, Resource) the pure `decide()` function judges.

Rules of thumb:
  * Everything goes through `AuthzService.check`, so every decision is logged and unknown actions are denied.
  * A user with NO membership row for the resource's org keeps today's behaviour: the owner of their own row may read it.
    (Every sign-in creates an owner membership, so this only matters for rows made before memberships existed or in tests.)
  * Only an actual membership can LOWER access (a viewer cannot start or save things); absence never denies an owner.
  * A thread with visibility "channel" is readable by the members listed in `channel_members` (and its author), nobody else.
"""

from sqlalchemy import select

from app.db.engine import Database
from app.db.models import ChannelMember, Task, Thread, User
from app.db.scoped import ScopedDatabase
from app.security.authz import AuthzService, Decision, Resource
from app.tenancy.service import PermissionDenied


class Access:
    def __init__(self, db: Database, authz: AuthzService):
        self.db, self.authz = db, authz

    async def _decide(self, user_id: str, org_id: str, action: str, r: Resource) -> Decision:
        p = await self.authz.principal(user_id, org_id)
        if p.role is None and r.owner_user_id == user_id:
            return Decision(True, "owner of their own row, no membership recorded")      # legacy: no membership row yet
        return await self.authz.check(p, action, r)

    async def default_org(self, user_id: str) -> str | None:
        async with self.db.session() as s:
            return await s.scalar(select(User.default_org_id).where(User.id == user_id))

    # ── reads ──
    async def can_read_task(self, user_id: str, task: Task) -> bool:
        d = await self._decide(user_id, task.org_id, "read_task", Resource("task", task.org_id, task.user_id, id=task.id))
        return d.allow

    async def can_read_thread(self, user_id: str, thread: Thread) -> bool:
        visibility = thread.visibility
        if visibility == "channel":
            async with ScopedDatabase(self.db, thread.org_id).session() as s:
                member = await s.scalar(select(ChannelMember.id).where(
                    ChannelMember.channel_id == thread.channel_id, ChannelMember.user_id == user_id))
            visibility = "org" if member else "private"          # decide() shares "org" threads with every member of the org
        d = await self._decide(user_id, thread.org_id, "read_thread",
                               Resource("thread", thread.org_id, thread.user_id, visibility=visibility, id=thread.id))
        return d.allow

    async def require_read_thread(self, user_id: str, thread_id: str) -> Thread:
        async with self.db.session() as s:           # the org is not known until the row is read; the decision then checks it
            t = await s.get(Thread, thread_id)
        if t is None or not await self.can_read_thread(user_id, t):
            raise PermissionDenied("not found")
        return t

    # ── actions on one's own credentials ──
    async def can_create_credential(self, user_id: str) -> Decision:
        org = await self.default_org(user_id)
        if org is None:
            return Decision(True, "no organisation recorded")
        p = await self.authz.principal(user_id, org)
        if p.role is None:
            return Decision(True, "no membership recorded")
        return await self.authz.check(p, "create_credential", Resource("credential", org, user_id))

    # ── channel membership (admins manage it) ──
    async def add_channel_member(self, *, org_id: str, channel_id: str, user_id: str, actor_id: str) -> None:
        p = await self.authz.principal(actor_id, org_id)
        await self.authz.require(p, "manage_members", Resource("org", org_id))
        if (await self.authz.principal(user_id, org_id)).role is None:
            raise PermissionDenied("only members of the organisation can join a channel")
        async with ScopedDatabase(self.db, org_id).session() as s:
            if await s.scalar(select(ChannelMember.id).where(ChannelMember.channel_id == channel_id,
                                                             ChannelMember.user_id == user_id)) is None:
                s.add(ChannelMember(org_id=org_id, channel_id=channel_id, user_id=user_id))

    async def remove_channel_member(self, *, org_id: str, channel_id: str, user_id: str, actor_id: str) -> None:
        p = await self.authz.principal(actor_id, org_id)
        await self.authz.require(p, "manage_members", Resource("org", org_id))
        async with ScopedDatabase(self.db, org_id).session() as s:
            row = await s.scalar(select(ChannelMember).where(ChannelMember.channel_id == channel_id,
                                                             ChannelMember.user_id == user_id))
            if row is not None:
                await s.delete(row)


def access() -> Access:
    """The request-time instance, built from the running app's services."""
    from app.core.runtime import runtime
    rt = runtime()
    return Access(rt.services.db, rt.services.authz)
