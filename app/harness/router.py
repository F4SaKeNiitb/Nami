"""Which tasks may use the exploration harness. Plain code over the access plan, never a model's opinion.

Fail closed: the answer is True only when EVERY condition holds, so any new field that makes a task more sensitive has to
be added here before it can reach the harness.
"""

from app.agent.schemas import AccessPlan, Intent


def explore_eligible(intent: Intent, access: AccessPlan) -> bool:
    """Read-only work on sites the user named, with nothing secret, no account, and nothing that needs approval."""
    return bool(
        access.allowed_domains
        and access.blocked is None
        and access.login == "none"
        and not access.credentials
        and not access.cards
        and not access.approval_actions
        and access.connect_site is None
        and access.ask is None
        and not intent.needs_login
        and not intent.actions
        and not intent.generate
    )
