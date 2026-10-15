"""Mints agent tokens only after the authorisation check passes ("mint after RBAC")."""

from app.security.authz import AuthzService, Principal, Resource
from app.security.identity import IdentityError, IdentityIssuer


class IdentityService:
    def __init__(self, issuer: IdentityIssuer, authz: AuthzService):
        self.issuer, self.authz = issuer, authz

    async def token_for_credential(self, *, principal: Principal, task_id: str, credential_id: str,
                                   credential_owner_id: str, role: str = "broker", ttl_s: int = 30) -> str:
        """One token, one credential, one use. Refused if this principal may not use that credential."""
        d = await self.authz.check(principal, "use_credential",
                                   Resource("credential", principal.org_id, credential_owner_id, id=credential_id))
        if not d.allow:
            raise IdentityError("not permitted")
        return self.issuer.mint(org=principal.org_id, user=principal.user_id, task=task_id, role=role, aud="vault",
                                scopes=[f"use_credential:{credential_id}"], ttl_s=ttl_s)
