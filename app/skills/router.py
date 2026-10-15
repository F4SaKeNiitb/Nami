"""'Learned workflows' panel API: list, approve and revoke what Nami has learned. Same cookie auth and CSRF guard as the vault."""

from chainlit.auth import get_current_user
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.runtime import runtime
from app.skills import exchange
from app.skills.service import Frozen, SkillError
from app.vault.router import same_origin

router = APIRouter(prefix="/chainlit/skills", tags=["skills"])


async def who(user=Depends(get_current_user)) -> tuple[str, str]:
    meta = (getattr(user, "metadata", None) or {}) if user else {}
    if not meta.get("user_id") or not meta.get("org_id"):
        raise HTTPException(401, "sign in first")
    return meta["user_id"], meta["org_id"]


def _public(sk) -> dict:
    return {"id": sk.id, "site": sk.site, "intent": sk.intent, "version": sk.version, "kind": sk.kind, "scope": sk.scope,
            "status": sk.status, "sha256": sk.content_sha256, "steps": sk.content.get("steps", []),
            "code": sk.content.get("code"), "domains": sk.manifest.get("domains", []), "risk": sk.manifest.get("risk_tier"),
            "scan": sk.scan, "success_count": sk.success_count, "created_at": sk.created_at.isoformat(),
            "last_used_at": sk.last_used_at.isoformat() if sk.last_used_at else None}


@router.get("")
async def list_skills(ident=Depends(who)):
    user_id, org_id = ident
    return [_public(s) for s in await runtime().orchestrator.skills.list_for(org_id=org_id, user_id=user_id)]


class Approve(BaseModel):
    expected_sha256: str | None = None


@router.post("/{skill_id}/approve", dependencies=[Depends(same_origin)])
async def approve(skill_id: str, body: Approve, ident=Depends(who)):
    try:
        return _public(await runtime().orchestrator.skills.approve(skill_id, actor_id=ident[0], org_id=ident[1], expected_sha256=body.expected_sha256))
    except Frozen:
        raise HTTPException(423, "skills are frozen") from None
    except SkillError:
        raise HTTPException(404, "not found") from None          # same answer for "not yours", "missing" and "not allowed"


@router.delete("/{skill_id}", dependencies=[Depends(same_origin)])
async def revoke(skill_id: str, ident=Depends(who)):
    try:
        await runtime().orchestrator.skills.revoke(skill_id, actor_id=ident[0], org_id=ident[1])
    except SkillError:
        raise HTTPException(404, "not found") from None
    return {"revoked": True}


@router.get("/{skill_id}/export")
async def export_skill(skill_id: str, ident=Depends(who)):
    try:
        return await exchange.export_skill(runtime().orchestrator.skills, skill_id, actor_id=ident[0], org_id=ident[1])
    except SkillError:
        raise HTTPException(404, "not found") from None


class Bundle(BaseModel):
    format: str = Field(max_length=40)
    skill: dict
    signature: str = Field(max_length=400)


@router.post("/import", dependencies=[Depends(same_origin)])
async def import_skill(body: Bundle, ident=Depends(who)):
    """Always lands as a candidate in the caller's own scope; the caller still approves it."""
    try:
        sk = await exchange.import_skill(runtime().orchestrator.skills, body.model_dump(), actor_id=ident[0], org_id=ident[1])
    except Frozen:
        raise HTTPException(423, "skills are frozen") from None
    except SkillError as e:
        raise HTTPException(422, str(e)) from None
    return _public(sk)
