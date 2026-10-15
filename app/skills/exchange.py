"""Skill export and import (OWASP AST10, cross-platform reuse).

An export carries the security metadata with the skill: content, manifest (domains, actions, risk tier, has_code), the
content hash and an ES256 signature over all of it, under a domain-separated message so a bundle signature can never be
replayed as a stored-row signature. An import is untrusted input, whatever the signature says:

  * unsigned, wrongly signed or edited bundles are refused (the signature covers every field that matters);
  * the manifest is required; a bundle that lost it is refused, never defaulted;
  * the shape is re-validated with the strict schemas and the scanner runs again;
  * the result is always a `candidate` owned by the importer, in user scope, re-signed as a new row. It needs the
    user's own approval (snippets: the exact content hash), exactly like a skill Nami learned itself.

The signature proves "this installation exported it unchanged". Bundles from another installation do not verify (their
key is not trusted here); trusting named publishers would need a key registry, which is not built.
"""

import base64
import binascii

from pydantic import ValidationError

from app.db.models import Skill
from app.skills import scanner
from app.skills.schemas import Manifest, SkillContent
from app.skills.service import Frozen, SkillError, SkillService
from app.skills.signing import canonical, content_sha256

FORMAT = "nami-skill/1"
MAX_BUNDLE_BYTES = 16_000
_DOMAIN = b"nami-skill-export/1\n"


def _message(skill: dict) -> bytes:
    return _DOMAIN + canonical(skill)


async def export_skill(svc: SkillService, skill_id: str, *, actor_id: str, org_id: str) -> dict:
    sk = await svc.get(skill_id, org_id)
    if sk.status != "approved" or (sk.user_id != actor_id and sk.scope != "org"):
        raise SkillError("not allowed")                      # only an approved skill the actor can already see
    if (why := svc._verify(sk)):
        raise SkillError(f"integrity check failed: {why}")
    body = {"site": sk.site, "intent": sk.intent, "version": sk.version, "kind": sk.kind, "content": sk.content,
            "sha256": sk.content_sha256, "manifest": sk.manifest}
    return {"format": FORMAT, "skill": body, "signature": base64.b64encode(svc.signer.sign(_message(body))).decode()}


async def import_skill(svc: SkillService, bundle: dict, *, actor_id: str, org_id: str) -> Skill:
    if svc.freeze:
        raise Frozen("skills are frozen")
    if not isinstance(bundle, dict) or bundle.get("format") != FORMAT:
        raise SkillError("not a skill bundle")
    if len(canonical(bundle)) > MAX_BUNDLE_BYTES:
        raise SkillError("bundle is too large")
    body, sig = bundle.get("skill"), bundle.get("signature")
    if not isinstance(body, dict) or not isinstance(sig, str):
        raise SkillError("bundle is unsigned")
    try:
        raw = base64.b64decode(sig, validate=True)
    except (binascii.Error, ValueError):
        raise SkillError("bad signature") from None
    if not svc.signer.verify(_message(body), raw):
        raise SkillError("signature does not verify")
    if not isinstance(body.get("manifest"), dict):
        raise SkillError("the bundle has no manifest")
    try:
        content = SkillContent.model_validate(body.get("content"))
        manifest = Manifest.model_validate(body["manifest"])
    except ValidationError:
        raise SkillError("the bundle does not match the skill format") from None
    if content_sha256(body["content"]) != body.get("sha256"):          # the hash was taken over the stored form, not the model's
        raise SkillError("content hash mismatch")
    if content.site != body.get("site") or content.intent != body.get("intent"):
        raise SkillError("bundle fields disagree")
    # the imported manifest is capped to what the content itself needs: has_code must match, domains are scanned below
    result = scanner.scan(content, manifest)
    return await svc.add_candidate(org_id=org_id, user_id=actor_id, content=content, manifest=manifest, scan=result,
                                   provenance={"imported_sha256": body["sha256"]})
