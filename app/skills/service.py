"""Skill lifecycle: propose -> scan -> (judge) -> sign -> approve -> load -> use -> drift/revoke.

Trust rules (all in code, none in a prompt):
  * a failed scan is `rejected`; nothing can approve it;
  * plain-text, user-scope skills that scan clean are auto-approved unless policy `skills_require_approval` is set;
  * snippets (code) need a human to approve the exact content hash; org-scope skills need an admin/owner;
  * a judge can only demote to `candidate`;
  * at load the signature and content hash are re-verified, status must be `approved`, and the effective manifest is
    manifest ∩ access plan, so a skill can never widen what a task may touch;
  * NAMI_SKILLS_FREEZE=1 refuses every write that would create or approve a skill.
"""

import logging
import re
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select

from app.agent.schemas import Action
from app.db.base import utcnow
from app.db.engine import Database
from app.db.scoped import ScopedDatabase
from app.db.models import Membership, Skill, SkillEvent
from app.skills import params as slots, scanner
from app.skills.judge import JudgeVerdict
from app.skills.schemas import Manifest, SkillContent
from app.skills.signing import Signer, content_sha256, signed_fields

log = logging.getLogger("nami.skills")
_WORD = re.compile(r"[a-z0-9]{3,}")


class SkillError(Exception):
    pass


class Frozen(SkillError):
    pass


@dataclass(frozen=True)
class LoadedSkill:
    id: str
    version: int
    site: str
    text: str                  # guidance to give the planner; never executed
    domains: list[str]         # effective manifest ∩ access plan


def _tokens(s: str) -> set[str]:
    return set(_WORD.findall(s.lower()))


class SkillService:
    def __init__(self, db: Database, signer: Signer, *, freeze: bool = False):
        self.db, self.signer, self.freeze = db, signer, freeze

    # ───────── events ─────────

    async def _event(self, skill_id: str, type_: str, task_id: str | None = None, **payload) -> None:
        async with self.db.session() as s:
            s.add(SkillEvent(skill_id=skill_id, task_id=task_id, type=type_, payload=payload))

    async def events(self, skill_id: str) -> list[SkillEvent]:
        async with self.db.session() as s:
            return list((await s.scalars(select(SkillEvent).where(SkillEvent.skill_id == skill_id)
                                         .order_by(SkillEvent.ts, SkillEvent.id))).all())

    # ───────── create ─────────

    def _message(self, sk: Skill) -> bytes:
        return signed_fields(skill_id=sk.id, org_id=sk.org_id, user_id=sk.user_id, scope=sk.scope, kind=sk.kind,
                             site=sk.site, intent=sk.intent, version=sk.version, sha256=sk.content_sha256, manifest=sk.manifest)

    async def propose(self, *, org_id: str, user_id: str, content: SkillContent, observed_domains: list[str],
                      plan_domains: list[str], actions: list[Action] | None = None, scope: str = "user",
                      require_approval: bool = False, verdict: JudgeVerdict | None = None,
                      provenance: dict | None = None, constraints: list[str] | None = None) -> Skill:
        """Create a new version. `observed_domains` is what the run actually visited; the manifest is capped by the plan."""
        if self.freeze:
            raise Frozen("skills are frozen")
        if constraints and not content.params and content.code is None:      # keep the user's own wording out of the saved steps
            new_steps, found = slots.parameterise(content.steps, constraints)
            if found:
                content = content.model_copy(update={"steps": new_steps, "params": found})
        domains = sorted(set(observed_domains) & set(plan_domains))
        has_code = content.code is not None
        manifest = Manifest(domains=domains, actions=actions or [], has_code=has_code,
                            risk_tier="high" if has_code else ("medium" if actions else "low"))
        result = scanner.scan(content, manifest)
        body = content.model_dump()
        async with self.db.session() as s:
            prev = await s.scalar(select(func.max(Skill.version)).where(
                Skill.org_id == org_id, Skill.user_id == user_id, Skill.site == content.site, Skill.intent == content.intent))
            sk = Skill(org_id=org_id, user_id=user_id, scope=scope, kind="snippet" if has_code else "workflow",
                       site=content.site, intent=content.intent, version=(prev or 0) + 1, content=body,
                       content_sha256=content_sha256(body), manifest=manifest.model_dump(), provenance=provenance or {},
                       scan=result.to_json(), status="candidate")
            if not result.passed:
                sk.status = "rejected"
            s.add(sk)
            await s.flush()
            sk.signature = self.signer.sign(self._message(sk))
            skill_id, status = sk.id, sk.status
        await self._event(skill_id, "created", version=(prev or 0) + 1)
        await self._event(skill_id, "scanned", passed=result.passed, rules=sorted({f.rule for f in result.findings}))
        if status == "rejected":
            return await self.get(skill_id)
        suspicious = verdict is not None and verdict.verdict == "suspicious"
        auto = (not has_code and scope == "user" and not require_approval and not suspicious)
        if auto:
            return await self._approve(skill_id, "system:auto")
        if suspicious:
            await self._event(skill_id, "judged", verdict="suspicious", reasons=verdict.reasons[:3])
        return await self.get(skill_id)

    async def add_candidate(self, *, org_id: str, user_id: str, content: SkillContent, manifest: Manifest,
                            scan: "scanner.ScanResult", provenance: dict) -> Skill:
        """An imported skill: new row, user scope, re-signed here, `candidate` (or `rejected` if the scan failed). Never auto-approved."""
        if self.freeze:
            raise Frozen("skills are frozen")
        body = content.model_dump()
        async with self.db.session() as s:
            prev = await s.scalar(select(func.max(Skill.version)).where(
                Skill.org_id == org_id, Skill.user_id == user_id, Skill.site == content.site, Skill.intent == content.intent))
            sk = Skill(org_id=org_id, user_id=user_id, scope="user", kind="snippet" if manifest.has_code else "workflow",
                       site=content.site, intent=content.intent, version=(prev or 0) + 1, content=body,
                       content_sha256=content_sha256(body), manifest=manifest.model_dump(), provenance=provenance,
                       scan=scan.to_json(), status="candidate" if scan.passed else "rejected")
            s.add(sk)
            await s.flush()
            sk.signature = self.signer.sign(self._message(sk))
            skill_id = sk.id
        await self._event(skill_id, "imported", sha256=provenance.get("imported_sha256"))
        await self._event(skill_id, "scanned", passed=scan.passed, rules=sorted({f.rule for f in scan.findings}))
        return await self.get(skill_id)

    async def get(self, skill_id: str, org_id: str | None = None) -> Skill:
        """With an org, another org's skill is simply not found. Without one (system jobs only), any org's."""
        sess = ScopedDatabase(self.db, org_id).session() if org_id else self.db.session()
        async with sess as s:
            sk = await s.get(Skill, skill_id)
            if sk is None:
                raise SkillError("not found")
            s.expunge(sk)
            return sk

    # ───────── approve / revoke ─────────

    async def _approve(self, skill_id: str, by: str) -> Skill:
        if self.freeze:
            raise Frozen("skills are frozen")
        async with self.db.session() as s:
            sk = await s.get(Skill, skill_id)
            if sk is None or sk.status in ("rejected", "revoked"):
                raise SkillError("cannot approve this skill")
            if not self.signer.verify(self._message(sk), sk.signature) or content_sha256(sk.content) != sk.content_sha256:
                raise SkillError("integrity check failed")
            sk.status, sk.approved_by, sk.approved_at, sk.last_verified_at = "approved", by, utcnow(), utcnow()
        await self._event(skill_id, "approved", by=by)
        return await self.get(skill_id)

    async def _role(self, org_id: str, user_id: str) -> str | None:
        async with self.db.session() as s:
            return await s.scalar(select(Membership.role).where(Membership.org_id == org_id, Membership.user_id == user_id))

    async def approve(self, skill_id: str, *, actor_id: str, org_id: str, expected_sha256: str | None = None) -> Skill:
        sk = await self.get(skill_id, org_id)
        role = await self._role(sk.org_id, actor_id)
        if role is None or role == "viewer":
            raise SkillError("not allowed")
        if sk.scope == "org":
            if role not in ("owner", "admin"):
                raise SkillError("only an admin can approve an org skill")
        elif sk.user_id != actor_id:
            raise SkillError("not allowed")
        if sk.kind == "snippet" and expected_sha256 != sk.content_sha256:
            raise SkillError("approving code requires the exact content hash you reviewed")
        return await self._approve(skill_id, actor_id)

    async def revoke(self, skill_id: str, *, actor_id: str, org_id: str) -> None:
        sk = await self.get(skill_id, org_id)
        role = await self._role(sk.org_id, actor_id)
        owner_ok = sk.user_id == actor_id and role not in (None,)
        admin_ok = sk.scope == "org" and role in ("owner", "admin")
        if not (owner_ok or admin_ok):
            raise SkillError("not allowed")
        async with self.db.session() as s:
            (await s.get(Skill, skill_id)).status = "revoked"       # revocation is allowed even when frozen
        await self._event(skill_id, "revoked", by=actor_id)

    async def list_for(self, *, org_id: str, user_id: str) -> list[Skill]:
        async with ScopedDatabase(self.db, org_id).session() as s:
            rows = await s.scalars(select(Skill).where(Skill.org_id == org_id, Skill.status != "revoked").where(
                (Skill.user_id == user_id) | (Skill.scope == "org")).order_by(Skill.created_at.desc()))
            return list(rows.all())

    # ───────── load / use ─────────

    def _verify(self, sk: Skill) -> str | None:
        if sk.status != "approved":
            return f"status is {sk.status}"
        if content_sha256(sk.content) != sk.content_sha256:
            return "content hash mismatch"
        if not self.signer.verify(self._message(sk), sk.signature):
            return "signature invalid"
        return None

    async def load_for(self, *, org_id: str, user_id: str, site: str, goal: str, plan_domains: list[str],
                       task_id: str | None = None, k: int = 2, constraints: list[str] | None = None) -> list[LoadedSkill]:
        """Approved, verified guidance for this site and goal. Text workflows only; snippets are never loaded here."""
        async with ScopedDatabase(self.db, org_id).session() as s:
            rows = list((await s.scalars(select(Skill).where(
                Skill.org_id == org_id, Skill.site == site, Skill.kind == "workflow", Skill.status == "approved")
                .where((Skill.user_id == user_id) | (Skill.scope == "org")))).all())
        want = _tokens(goal)
        newest: dict[tuple, Skill] = {}
        for sk in rows:                                      # last approved version per (owner, intent)
            key = (sk.user_id, sk.intent)
            if key not in newest or sk.version > newest[key].version:
                newest[key] = sk
        scored = sorted(newest.values(), key=lambda sk: -len(want & _tokens(sk.intent)))
        out: list[LoadedSkill] = []
        for sk in scored:
            if not want & _tokens(sk.intent):
                continue
            why = self._verify(sk)
            if why:
                await self._event(sk.id, "blocked", task_id, reason=why)
                continue
            domains = sorted(set(sk.manifest["domains"]) & set(plan_domains))      # never wider than the plan
            if sk.site not in domains:
                await self._event(sk.id, "blocked", task_id, reason="site outside the access plan")
                continue
            c = SkillContent.model_validate(sk.content)
            steps, _ = scanner.strip_placeholders("; ".join(c.steps))
            steps = slots.fill(steps, c.params, constraints or [])
            text = f"On {c.site}, to {c.intent}: {steps}" + (f". Watch out: {'; '.join(c.pitfalls)}" if c.pitfalls else "")
            await self._event(sk.id, "loaded", task_id)
            out.append(LoadedSkill(sk.id, sk.version, sk.site, text, domains))
            if len(out) >= k:
                break
        return out

    async def record_use(self, skill_id: str, *, observed_domains: list[str], verified: bool, task_id: str | None = None) -> None:
        """After a run that used the skill: success count, and demote to candidate if behaviour left the manifest."""
        sk = await self.get(skill_id)
        extra = sorted(set(observed_domains) - set(sk.manifest["domains"]))
        async with self.db.session() as s:
            row = await s.get(Skill, skill_id)
            row.last_used_at = utcnow()
            if verified:
                row.success_count += 1
            if extra and row.status == "approved":
                row.status = "candidate"
        if extra:
            await self._event(skill_id, "drifted", task_id, extra_domains=extra)

    # ───────── nightly ─────────

    async def reverify_all(self, *, max_age: timedelta = timedelta(days=1)) -> dict[str, int]:
        """Re-hash, re-verify and re-scan every approved skill. Anything stale or altered is demoted and logged."""
        counts = {"checked": 0, "demoted": 0}
        async with self.db.session() as s:
            ids = list((await s.scalars(select(Skill.id).where(Skill.status == "approved"))).all())
        for sid in ids:
            sk = await self.get(sid)
            counts["checked"] += 1
            why = self._verify(sk)
            if why is None:
                res = scanner.scan(SkillContent.model_validate(sk.content), Manifest.model_validate(sk.manifest))
                if not res.passed:
                    why = "rescan failed: " + ",".join(sorted({f.rule for f in res.findings}))
            if why is None and sk.last_verified_at and utcnow() - sk.last_verified_at > max_age * 7:
                why = "verification is stale"
            async with self.db.session() as s:
                row = await s.get(Skill, sid)
                if why:
                    row.status = "candidate"
                else:
                    row.last_verified_at = utcnow()
            if why:
                counts["demoted"] += 1
                await self._event(sid, "drifted", reason=why)
        return counts


async def nightly_reverify(skills: "SkillService", *, interval_s: float = 6 * 3600, stop=None) -> None:
    """Background loop: re-hash, re-verify and re-scan every approved skill. Errors are logged, never raised."""
    import asyncio
    while stop is None or not stop.is_set():
        try:
            counts = await skills.reverify_all()
            if counts["demoted"]:
                log.warning("skill re-verification demoted %d of %d skills", counts["demoted"], counts["checked"])
        except Exception:
            log.warning("skill re-verification failed", exc_info=True)
        try:
            if stop is None:
                await asyncio.sleep(interval_s)
            else:
                await asyncio.wait_for(stop.wait(), interval_s)
        except asyncio.TimeoutError:
            pass
