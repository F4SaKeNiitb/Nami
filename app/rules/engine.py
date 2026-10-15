"""Resolve the rules in force for (firm, user, site).

Layering, per key:
  1. base   = the user's own rules if any, else the firm's soft rules (defaults)
              within a layer, a site-specific rule beats a general one
  2. final  = base combined with the firm's hard rules
              policies:    restrictively (min spend, union of approvals/denies...)
              preferences: the hard value wins
  instructions are additive text: firm hard first, then firm soft, then the user's.

Only policies are machine-enforced (by the guard). Preferences and instructions are
rendered into the prompt; that's guidance, not a guarantee.
"""

import hashlib
import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import or_, select

from app.cache.exact import ExactCache
from app.db.engine import Database
from app.db.models import Org, Rule, User
from app.db.scoped import ScopedDatabase
from app.rules.policies import DEFAULT_POLICIES, POLICIES, restrict, validate_policy
from app.tenancy.service import PermissionDenied, role_in


@dataclass(frozen=True)
class Instruction:
    text: str
    source: str  # "firm" | "you"
    hard: bool


@dataclass(frozen=True)
class EffectiveRules:
    org_id: str
    user_id: str
    site: str | None
    version: str
    policies: dict[str, Any] = field(default_factory=dict)
    preferences: dict[str, Any] = field(default_factory=dict)
    instructions: list[Instruction] = field(default_factory=list)

    # ---- guard helpers ----
    def domain_allowed(self, etld1: str) -> bool:
        etld1 = etld1.lower()
        if etld1 in self.policies.get("domain_deny", []):
            return False
        allow = self.policies.get("domain_allow")
        return allow is None or etld1 in allow

    def needs_approval(self, category: str) -> bool:
        return category in self.policies.get("require_approval", [])

    def blocked(self, category: str) -> bool:
        return category in self.policies.get("blocked_actions", [])

    def within_spend(self, amount: float, currency: str) -> bool:
        limit = self.policies.get("spend_limit", {}).get(currency.upper())
        return limit is None or amount <= limit

    # ---- prompt rendering (deterministic, so it's prompt-cache friendly) ----
    def prompt_block(self) -> str:
        lines = ["## Rules in force"]
        for i in self.instructions:
            tag = "Firm policy (must follow)" if i.hard else ("Firm guideline" if i.source == "firm" else "User rule")
            lines.append(f"- [{tag}] {i.text}")
        for k in sorted(self.preferences):
            lines.append(f"- [Preference] {k}: {json.dumps(self.preferences[k], sort_keys=True)}")
        for k in sorted(self.policies):
            lines.append(f"- [Enforced] {k}: {json.dumps(self.policies[k], sort_keys=True)}")
        return "\n".join(lines)

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "EffectiveRules":
        return cls(**{**d, "instructions": [Instruction(**i) for i in d["instructions"]]})


def _pick_layer(rules: list[Rule], site: str | None) -> list[Rule]:
    specific = [r for r in rules if site and r.site_etld1 == site]
    return specific or [r for r in rules if r.site_etld1 is None]


class RulesService:
    def __init__(self, db: Database, cache: ExactCache, cache_ttl_s: int = 3600):
        self.db, self.cache, self.ttl = db, cache, cache_ttl_s

    # ───────── writes ─────────
    async def add_rule(self, *, actor_id: str, org_id: str, kind: str, key: str, value: Any,
                       user_id: str | None = None, enforcement: str = "soft", site: str | None = None,
                       priority: int = 0, description: str | None = None) -> Rule:
        if kind == "policy":
            value = validate_policy(key, value)
        elif kind == "instruction" and not (isinstance(value, str) and value.strip()):
            raise ValueError("instruction value must be non-empty text")
        if user_id is not None and enforcement == "hard":
            raise ValueError("hard enforcement is only for firm-wide rules")
        async with self.db.session() as s:
            await self._authorise(s, actor_id, org_id, user_id)
            rule = Rule(org_id=org_id, user_id=user_id, kind=kind, key=key, value=value,
                        enforcement=enforcement, site_etld1=site.lower() if site else None,
                        priority=priority, description=description, created_by=actor_id)
            s.add(rule)
            await self._bump(s, org_id, user_id)
            return rule

    async def set_enabled(self, *, actor_id: str, rule_id: str, enabled: bool) -> None:
        async with self.db.session() as s:
            rule = await s.get(Rule, rule_id)
            if rule is None:
                raise LookupError(rule_id)
            await self._authorise(s, actor_id, rule.org_id, rule.user_id)
            rule.enabled = enabled
            await self._bump(s, rule.org_id, rule.user_id)

    async def delete_rule(self, *, actor_id: str, rule_id: str) -> None:
        async with self.db.session() as s:
            rule = await s.get(Rule, rule_id)
            if rule is None:
                return
            await self._authorise(s, actor_id, rule.org_id, rule.user_id)
            await self._bump(s, rule.org_id, rule.user_id)
            await s.delete(rule)

    async def _authorise(self, s, actor_id: str, org_id: str, target_user_id: str | None) -> None:
        role = await role_in(s, org_id, actor_id)
        if role is None:
            raise PermissionDenied("not a member of this firm")
        if role == "viewer":
            raise PermissionDenied("viewers cannot change rules")
        is_admin = role in ("owner", "admin")
        if target_user_id is None and not is_admin:
            raise PermissionDenied("only firm admins can set firm-wide rules")
        if target_user_id is not None:
            if target_user_id != actor_id and not is_admin:
                raise PermissionDenied("you can only set your own rules")
            if await role_in(s, org_id, target_user_id) is None:
                raise PermissionDenied("target user is not in this firm")

    @staticmethod
    async def _bump(s, org_id: str, user_id: str | None) -> None:
        # Version bump in the same transaction as the change → caches can't serve stale rules.
        target = await s.get(User, user_id) if user_id else await s.get(Org, org_id)
        target.rules_version += 1

    # ───────── reads ─────────
    async def resolve(self, *, org_id: str, user_id: str, site: str | None = None) -> EffectiveRules:
        site = site.lower() if site else None
        async with self.db.session() as s:
            if await role_in(s, org_id, user_id) is None:
                raise PermissionDenied("not a member of this firm")
            org_v = await s.scalar(select(Org.rules_version).where(Org.id == org_id))
            user_v = await s.scalar(select(User.rules_version).where(User.id == user_id))
        version = hashlib.sha256(f"{org_id}:{org_v}:{user_id}:{user_v}:{site}".encode()).hexdigest()[:16]

        cached = await self.cache.get_json("rules", version)
        if cached is not None:
            return EffectiveRules.from_json(cached)

        async with ScopedDatabase(self.db, org_id).session() as s:
            rows = list(await s.scalars(select(Rule).where(
                Rule.org_id == org_id, Rule.enabled.is_(True),
                or_(Rule.user_id.is_(None), Rule.user_id == user_id),
                or_(Rule.site_etld1.is_(None), Rule.site_etld1 == site),
            ).order_by(Rule.priority.desc(), Rule.created_at, Rule.id)))

        eff = self._merge(rows, org_id, user_id, site, version)
        await self.cache.set_json("rules", version, eff.to_json(), self.ttl)
        return eff

    @staticmethod
    def _merge(rows: list[Rule], org_id: str, user_id: str, site: str | None, version: str) -> EffectiveRules:
        layers: dict[tuple[str, str], dict[str, list[Rule]]] = defaultdict(lambda: defaultdict(list))
        for r in rows:
            layer = "user" if r.user_id else ("firm_hard" if r.enforcement == "hard" else "firm_soft")
            layers[(r.kind, r.key)][layer].append(r)

        policies: dict[str, Any] = {}
        preferences: dict[str, Any] = {}
        for (kind, key), by_layer in layers.items():
            if kind == "instruction":
                continue
            base_rules = _pick_layer(by_layer.get("user", []), site) or _pick_layer(by_layer.get("firm_soft", []), site)
            hard_rules = by_layer.get("firm_hard", [])  # every hard rule applies, general and site
            if kind == "policy" and key in POLICIES:
                value = None
                for r in base_rules + hard_rules:
                    value = r.value if value is None else restrict(key, value, r.value)
                if value is not None:
                    policies[key] = value
            elif kind == "preference":
                chosen = (_pick_layer(hard_rules, site) or base_rules)
                if chosen:
                    preferences[key] = chosen[0].value  # highest priority first

        for key, default in DEFAULT_POLICIES.items():
            policies[key] = restrict(key, policies[key], default) if key in policies else default

        order = {"firm_hard": 0, "firm_soft": 1, "user": 2}
        instr = sorted(
            ((order["user" if r.user_id else ("firm_hard" if r.enforcement == "hard" else "firm_soft")], r)
             for r in rows if r.kind == "instruction"),
            key=lambda t: t[0])
        instructions = [Instruction(r.value, "you" if r.user_id else "firm", r.enforcement == "hard")
                        for _, r in instr]
        return EffectiveRules(org_id, user_id, site, version, policies, preferences, instructions)
