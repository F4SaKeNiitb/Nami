"""Exhaustive matrix over the pure authorisation function. Exit 1 on any violation."""

import itertools

from app.security.authz import ROLES, Principal, Resource, decide

ACTIONS = ["start_task", "resume_task", "cancel_task", "read_task", "read_thread", "use_credential", "revoke_credential",
           "read_credential", "revoke_session", "read_personal_memory", "read_org_memory", "write_org_memory",
           "manage_org_rules", "manage_members", "manage_own_rules", "delete_org"]
# revoke_* is deliberately open to viewers for their own resources (removing access is a safety action)
MUTATING = {"start_task", "resume_task", "cancel_task", "use_credential",
            "write_org_memory", "manage_org_rules", "manage_members", "manage_own_rules", "delete_org"}
OWNER_ONLY = {"resume_task", "cancel_task", "use_credential", "revoke_credential", "read_credential", "revoke_session",
              "read_personal_memory"}
bad: list[str] = []
n = 0


def expect(cond: bool, msg: str) -> None:
    global n
    n += 1
    if not cond:
        bad.append(msg)


for role, action, vis in itertools.product(ROLES, ACTIONS, ["private", "channel", "org"]):
    me = Principal("u1", "orgA", role)
    own = Resource("thread", "orgA", "u1", vis)
    other = Resource("thread", "orgA", "u2", vis)
    foreign = Resource("thread", "orgB", "u1", vis)           # even if the user id matches
    tag = f"{role}/{action}/{vis}"
    expect(not decide(me, action, foreign).allow, f"cross-org allowed: {tag}")
    expect(not decide(Principal("u1", "orgA", None), action, own).allow, f"non-member allowed: {tag}")
    if action in OWNER_ONLY:
        expect(not decide(me, action, other).allow, f"owner-only action allowed on someone else's resource: {tag}")
    if role == "viewer" and action in MUTATING:
        expect(not decide(me, action, own).allow, f"viewer mutated: {tag}")
    if action == "read_task" or (action == "read_thread" and vis != "org"):
        expect(not decide(me, action, other).allow, f"private resource readable by another member/admin: {tag}")
    if role in ("admin", "owner") and action in ("read_task", "read_personal_memory", "use_credential") or \
            (role in ("admin", "owner") and action == "read_thread" and vis != "org"):
        expect(not decide(me, action, other).allow, f"admin reached a private resource: {tag}")
    if action in ("revoke_credential", "revoke_session"):
        expect(decide(me, action, own).allow, f"owner cannot revoke their own access: {tag}")

expect(not decide(Principal("u1", "orgA", "owner"), "teleport", Resource("org", "orgA", "u1")).allow, "unknown action allowed")
expect(not decide(Principal("u1", "orgA", "superuser"), "start_task", Resource("org", "orgA")).allow, "unknown role allowed")
expect(decide(Principal("u1", "orgA", "member"), "start_task", Resource("org", "orgA")).allow, "member cannot start a task")
expect(decide(Principal("u1", "orgA", "admin"), "manage_org_rules", Resource("org_rules", "orgA")).allow, "admin cannot manage rules")
expect(not decide(Principal("u1", "orgA", "member"), "manage_org_rules", Resource("org_rules", "orgA")).allow, "member managed firm rules")
expect(decide(Principal("u1", "orgA", "viewer"), "read_org_memory", Resource("org_memory", "orgA")).allow, "viewer cannot read org memory")
expect(decide(Principal("u1", "orgA", "member"), "read_thread", Resource("thread", "orgA", "u2", "org")).allow, "org-shared thread unreadable")

print(f"{n - len(bad)}/{n} authorisation checks passed")
for b in bad[:20]:
    print("  FAIL", b)
raise SystemExit(1 if bad else 0)
