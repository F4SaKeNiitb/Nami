"""Browser providers. One interface for Kernel (production) and local Chromium (dev, tests).

Rules owned by the base class so no provider can forget them:
  * a user's profile has exactly one writer at a time (a second `open` fails fast, unless the caller says a profile-less browser
    will do: `scratch_if_busy=True`, which is how one user runs several tasks at once; the logins come from encrypted snapshots);
  * `browser_session()` always closes, even when the task raises, which also releases the lock.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass


class ProfileBusy(Exception):
    """The user's browser profile is already open. Kernel allows one writer per profile."""


@dataclass(frozen=True)
class BrowserSession:
    session_id: str
    user_id: str
    cdp_url: str
    live_view_url: str | None = None


class BrowserProvider(ABC):
    def __init__(self) -> None:
        self._open_by_user: dict[str, str] = {}   # user_id -> session_id (single replica, in-process): the browser that holds the profile
        self._scratch: set[str] = set()           # session ids of the extra, profile-less browsers

    async def open(self, user_id: str, *, headless: bool = True, scratch_if_busy: bool = False) -> BrowserSession:
        if user_id in self._open_by_user:
            if not scratch_if_busy:
                raise ProfileBusy(f"profile for {user_id} is in use")
            session = await self._open(user_id, headless=headless, scratch=True)      # saves nothing back to the profile
            self._scratch.add(session.session_id)
            return session
        self._open_by_user[user_id] = "pending"       # claim before any await: no race
        try:
            session = await self._open(user_id, headless=headless)
        except BaseException:
            self._open_by_user.pop(user_id, None)
            raise
        self._open_by_user[user_id] = session.session_id
        return session

    async def close(self, session: BrowserSession) -> None:
        try:
            await self._close(session)
        finally:
            if session.session_id in self._scratch:
                self._scratch.discard(session.session_id)
                return
            if self._open_by_user.get(session.user_id) == session.session_id:
                del self._open_by_user[session.user_id]

    def is_open(self, user_id: str) -> bool:
        return user_id in self._open_by_user

    @abstractmethod
    async def _open(self, user_id: str, *, headless: bool, scratch: bool = False) -> BrowserSession:
        """`scratch=True`: a throwaway browser with no saved profile (the profile is held by another of the user's browsers)."""

    @abstractmethod
    async def _close(self, session: BrowserSession) -> None:
        """Must persist the profile (Kernel: delete_by_id writes it back)."""


@asynccontextmanager
async def browser_session(provider: BrowserProvider, user_id: str, *, headless: bool = True) -> AsyncIterator[BrowserSession]:
    session = await provider.open(user_id, headless=headless)
    try:
        yield session
    finally:
        await provider.close(session)
