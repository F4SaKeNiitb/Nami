"""Which Nami user a Slack account is. There is one step and it is on the Slack side: the email on the person's Slack profile is
matched to the Nami user (signed in with Google at least once) who has that email, and the link is stored.

  Slack DM  ->  Slack profile email (users.info, scope users:read.email)  ->  Nami user with that email  ->  row in slack_links

Trade-off, on purpose: Slack lets a workspace admin set any email on an account, so this trusts the workspace's admins. That is
fine for one company workspace the bot owner controls; do not install the bot into a workspace whose admins you do not trust.
An email that no Nami user has is never linked and never creates a user.
"""

import asyncio
import secrets
import time
from dataclasses import dataclass

from sqlalchemy import delete, func, select

from app.db.models import SlackLink, User

MAX_OPEN_CODES = 1000
ADD_TTL_SECONDS = 1800


@dataclass(frozen=True)
class AddRequest:
    """What a one-time "add it to the vault" page may save: for whom, for which site, which kinds. All fixed on the server."""
    user_id: str
    site: str
    kinds: tuple[str, ...]


@dataclass(frozen=True)
class SolveRequest:
    """A one-time link to the secure browser window for one waiting task (a CAPTCHA, a passkey): for whom, which task, which site."""
    user_id: str
    task_id: str
    site: str


@dataclass(frozen=True)
class Linked:
    user_id: str
    org_id: str


class SlackLinks:
    def __init__(self, db, *, clock=time.monotonic):
        self.db, self.clock = db, clock
        self._adds: dict[str, tuple[AddRequest, float]] = {}      # token -> (what it may save, expires). Memory only.
        self._solves: dict[str, tuple[SolveRequest, float]] = {}  # token -> (the window it opens, expires). Memory only.
        self._shapes: dict[str, asyncio.Future] = {}              # token -> the live look at that site's sign-in page, started when the link is made

    async def link(self, team_id: str, slack_user_id: str, user_id: str) -> None:
        """Tie that Slack account to this Nami user. A Slack account has one Nami user; linking again replaces it."""
        async with self.db.session() as s:
            await s.execute(delete(SlackLink).where(SlackLink.team_id == team_id, SlackLink.slack_user_id == slack_user_id))
            s.add(SlackLink(team_id=team_id, slack_user_id=slack_user_id, user_id=user_id))

    async def link_by_email(self, team_id: str, slack_user_id: str, email: str) -> Linked | None:
        """Link to the Nami user whose email is this one (case-insensitive). None when there is no such user: nobody is created here."""
        email = (email or "").strip().lower()
        if not email or "@" not in email:
            return None
        async with self.db.session() as s:
            row = (await s.execute(select(User.id).where(func.lower(User.email) == email))).first()
        if not row:
            return None
        await self.link(team_id, slack_user_id, row[0])
        return await self.resolve(team_id, slack_user_id)

    def begin_add(self, user_id: str, site: str, kinds: list[str]) -> str:
        """A link to a small page that saves one login or key for this user and site, and nothing else. The token (not the URL's
        other parts) is what carries the site and kinds, so a link cannot be edited to save something for a different site."""
        now = self.clock()
        req = AddRequest(user_id, site, tuple(kinds))
        keep = {t: v for t, v in self._adds.items() if v[1] > now and (v[0].user_id, v[0].site) != (user_id, site)}
        for t in set(self._adds) - set(keep):
            self._shapes.pop(t, None)
        self._adds = keep
        if len(self._adds) >= MAX_OPEN_CODES:
            self._adds.pop(next(iter(self._adds)))
        token = secrets.token_urlsafe(24)
        self._adds[token] = (req, now + ADD_TTL_SECONDS)
        return token

    def peek_add(self, token: str) -> AddRequest | None:
        item = self._adds.get(token or "")
        return item[0] if item and item[1] > self.clock() else None

    def begin_solve(self, user_id: str, task_id: str, site: str) -> str:
        now = self.clock()
        self._solves = {t: v for t, v in self._solves.items() if v[1] > now and v[0].task_id != task_id}
        if len(self._solves) >= MAX_OPEN_CODES:
            self._solves.pop(next(iter(self._solves)))
        token = secrets.token_urlsafe(24)
        self._solves[token] = (SolveRequest(user_id, task_id, site), now + ADD_TTL_SECONDS)
        return token

    def peek_solve(self, token: str) -> SolveRequest | None:
        item = self._solves.get(token or "")
        return item[0] if item and item[1] > self.clock() else None

    def watch_shape(self, token: str, coro) -> None:
        """Start looking at the site's sign-in page now, so the answer is usually ready by the time the link is opened."""
        self._shapes[token] = asyncio.ensure_future(coro)

    async def shape_of(self, token: str, timeout: float = 25):
        fut = self._shapes.get(token or "")
        if fut is None:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout)
        except Exception:
            return None

    def finish_add(self, token: str) -> None:
        self._adds.pop(token or "", None)
        self._shapes.pop(token or "", None)

    async def resolve(self, team_id: str, slack_user_id: str) -> Linked | None:
        async with self.db.session() as s:
            row = (await s.execute(select(SlackLink.user_id, User.default_org_id).join(User, User.id == SlackLink.user_id).where(
                SlackLink.team_id == team_id, SlackLink.slack_user_id == slack_user_id))).first()
        return Linked(row[0], row[1]) if row and row[1] else None

    async def unlink(self, team_id: str, slack_user_id: str) -> bool:
        async with self.db.session() as s:
            res = await s.execute(delete(SlackLink).where(SlackLink.team_id == team_id, SlackLink.slack_user_id == slack_user_id))
            return bool(res.rowcount)
