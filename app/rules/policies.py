"""Machine-enforced policy keys and how two values for the same key combine.

`restrict(a, b)` must return a value at least as strict as both. The resolver uses it to
layer firm *hard* rules on top of anything a user sets, so a user can tighten firm
policy but never loosen it.
"""

from collections.abc import Callable
from typing import Any


def _min_spend(a: dict, b: dict) -> dict:
    # {"INR": 2000, "USD": 25}: per-currency minimum; a currency set by either side applies
    out = dict(a)
    for cur, amt in b.items():
        out[cur] = min(out[cur], amt) if cur in out else amt
    return out


def _union(a: list, b: list) -> list:
    return sorted(set(a) | set(b))


def _intersect(a: list, b: list) -> list:
    return sorted(set(a) & set(b))


def _validate_spend(v: Any) -> dict:
    if not isinstance(v, dict) or not v or not all(
            isinstance(k, str) and len(k) == 3 and isinstance(x, (int, float)) and x >= 0
            for k, x in v.items()):
        raise ValueError('spend_limit must look like {"INR": 2000}')
    return {k.upper(): float(x) for k, x in v.items()}


def _validate_str_list(v: Any) -> list:
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise ValueError("expected a non-empty list of strings")
    return sorted({x.lower().strip() for x in v})


def _validate_bool(v: Any) -> bool:
    if not isinstance(v, bool):
        raise ValueError("expected true/false")
    return v


def _validate_minutes(v: Any) -> int:
    if not isinstance(v, int) or not 1 <= v <= 24 * 60:
        raise ValueError("expected minutes between 1 and 1440")
    return v


ACTION_CATEGORIES = {"purchase", "send_message", "delete", "account_change", "submit_form", "deploy"}


def _validate_actions(v: Any) -> list:
    acts = _validate_str_list(v)
    if bad := set(acts) - ACTION_CATEGORIES:
        raise ValueError(f"unknown action categories {sorted(bad)}; allowed: {sorted(ACTION_CATEGORIES)}")
    return acts


# key → (validator, restrictive merge)
POLICIES: dict[str, tuple[Callable[[Any], Any], Callable[[Any, Any], Any]]] = {
    "spend_limit": (_validate_spend, _min_spend),
    "require_approval": (_validate_actions, _union),
    "blocked_actions": (_validate_actions, _union),
    "domain_deny": (_validate_str_list, _union),
    "domain_allow": (_validate_str_list, _intersect),   # unset = no allow-list
    "allow_card_use": (_validate_bool, lambda a, b: a and b),
    "allow_stored_passwords": (_validate_bool, lambda a, b: a and b),
    "max_task_minutes": (_validate_minutes, min),
    "skills_require_approval": (_validate_bool, lambda a, b: a or b),   # any layer asking for approval wins
}

# Applied when no rule sets a key. Approval for purchases/sends/deletes is on by default.
DEFAULT_POLICIES: dict[str, Any] = {
    "require_approval": ["account_change", "delete", "purchase", "send_message"],
}


def validate_policy(key: str, value: Any) -> Any:
    if key not in POLICIES:
        raise ValueError(f"unknown policy '{key}'; known: {sorted(POLICIES)}")
    return POLICIES[key][0](value)


def restrict(key: str, a: Any, b: Any) -> Any:
    return POLICIES[key][1](a, b)
