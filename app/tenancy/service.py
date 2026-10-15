import re

from sqlalchemy import select

from app.db.engine import Database
from app.db.models import Membership, Org, User


class PermissionDenied(Exception):
    pass


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "org"


class TenancyService:
    def __init__(self, db: Database):
        self.db = db

    async def ensure_user(self, *, google_sub: str, email: str, name: str | None = None) -> User:
        """Called from the Google OAuth callback. First sign-in creates a personal org."""
        async with self.db.session() as s:
            user = await s.scalar(select(User).where(User.google_sub == google_sub))
            if user:
                return user
            user = User(google_sub=google_sub, email=email, name=name)
            s.add(user)
            await s.flush()
            org = Org(name=f"{name or email} (personal)", slug=f"personal-{user.id[:12]}", is_personal=True)
            s.add(org)
            await s.flush()
            s.add(Membership(org_id=org.id, user_id=user.id, role="owner"))
            user.default_org_id = org.id
            return user

    async def create_org(self, *, name: str, owner_id: str) -> Org:
        async with self.db.session() as s:
            org = Org(name=name, slug=f"{_slug(name)}-{owner_id[:6]}")
            s.add(org)
            await s.flush()
            s.add(Membership(org_id=org.id, user_id=owner_id, role="owner"))
            return org

    async def add_member(self, *, org_id: str, user_id: str, role: str, actor_id: str) -> None:
        async with self.db.session() as s:
            if await role_in(s, org_id, actor_id) not in ("owner", "admin"):
                raise PermissionDenied("only firm admins can add members")
            s.add(Membership(org_id=org_id, user_id=user_id, role=role))


async def role_in(session, org_id: str, user_id: str) -> str | None:
    return await session.scalar(
        select(Membership.role).where(Membership.org_id == org_id, Membership.user_id == user_id))
