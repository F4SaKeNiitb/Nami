"""Who may do what. One pure decision function, one service that records every decision.

Order of checks, always: same organisation first, then role, then ownership. Unknown actions are denied.
Private means private: an org admin cannot read another member's private thread, task or memory.
Credentials and sessions are never shared by role: only their owner can use or revoke them.
"""

from dataclasses import dataclass

from sqlalchemy import select

from app.db.engine import Database
from app.db.models import AuthzLog, Membership
from app.telemetry import tel
from app.tenancy.service import PermissionDenied

ROLES = ("owner", "admin", "member", "viewer")
_ADMINS = {"owner", "admin"}
_DOERS = {"owner", "admin", "member"}          # may start tasks and change their own settings


@dataclass(frozen=True)
class Principal:
    user_id: str
    org_id: str
    role: str | None            # None = not a member of org_id


@dataclass(frozen=True)
class Resource:
    kind: str                    # task | thread | credential | session | org | org_rules | org_memory | personal_memory
    org_id: str
    owner_user_id: str | None = None
    visibility: str = "private"  # private | channel | org   (threads only)
    id: str = ""

    def label(self) -> str:
        return f"{self.kind}:{self.id or '-'}"


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str


def _deny(why: str) -> Decision:
    return Decision(False, why)


def _is_owner(p: Principal, r: Resource) -> bool:
    return r.owner_user_id is not None and r.owner_user_id == p.user_id


def decide(p: Principal, action: str, r: Resource) -> Decision:
    if p.role is None:
        return _deny("not a member of this organisation")
    if r.org_id != p.org_id:
        return _deny("different organisation")
    if p.role not in ROLES:
        return _deny("unknown role")

    if action == "start_task":
        return Decision(True, "ok") if p.role in _DOERS else _deny("viewers cannot start tasks")

    if action in ("resume_task", "cancel_task"):
        if p.role not in _DOERS:
            return _deny("viewers cannot control tasks")
        return Decision(True, "ok") if _is_owner(p, r) else _deny("only the task owner can do this")

    if action in ("read_task", "read_thread"):
        if _is_owner(p, r):
            return Decision(True, "ok")
        if action == "read_thread" and r.visibility == "org":      # tasks have no sharing; only threads can be shared
            return Decision(True, "shared with the organisation")
        return _deny("private to its owner")                   # admins included, by design

    # Removing your own access is a safety action, so it is open to every role (including viewers).
    if action in ("use_credential", "revoke_credential", "read_credential", "revoke_session", "read_personal_memory"):
        if action == "use_credential" and p.role not in _DOERS:
            return _deny("viewers cannot use credentials")
        return Decision(True, "ok") if _is_owner(p, r) else _deny("credentials and personal memory are owner-only")

    if action == "create_credential":                          # saving a login or key: members and up; viewers are read-only
        return Decision(True, "ok") if p.role in _DOERS else _deny("viewers cannot save credentials")

    if action == "read_org_memory":
        return Decision(True, "ok")
    if action in ("write_org_memory", "manage_org_rules", "manage_members"):
        return Decision(True, "ok") if p.role in _ADMINS else _deny("admins only")
    if action == "manage_own_rules":
        return Decision(True, "ok") if p.role in _DOERS else _deny("viewers cannot change rules")
    if action == "delete_org":
        return Decision(True, "ok") if p.role == "owner" else _deny("owner only")

    return _deny("unknown action")                             # fail closed


class AuthzService:
    def __init__(self, db: Database):
        self.db = db

    async def principal(self, user_id: str, org_id: str) -> Principal:
        async with self.db.session() as s:
            role = await s.scalar(select(Membership.role).where(Membership.org_id == org_id, Membership.user_id == user_id))
        return Principal(user_id, org_id, role)

    async def check(self, p: Principal, action: str, r: Resource) -> Decision:
        d = decide(p, action, r)
        with tel.span("authz.check", org_id=p.org_id, user_id=p.user_id, **{"authz.action": action, "authz.decision": "allow" if d.allow else "deny"}):
            if not d.allow:
                tel.inc("authz_denied_total", action=action)
        try:
            async with self.db.session() as s:
                s.add(AuthzLog(org_id=p.org_id, user_id=p.user_id, role=p.role, action=action, resource=r.label(),
                               allowed=d.allow, reason=d.reason))
        except Exception:      # logging must not turn a deny into an allow, nor crash the request
            pass
        return d

    async def require(self, p: Principal, action: str, r: Resource) -> None:
        d = await self.check(p, action, r)
        if not d.allow:
            raise PermissionDenied(d.reason)
