"""Explicit memory commands typed by the user: the only way anything enters long-term memory.

Memory is written from the user's own words and nothing else. Page text, tool output and model output
never reach `remember()`, so a web page cannot plant a "fact" that later steers a task (OWASP ASI06).

  "remember that I'm vegetarian"      -> remember
  "remember: deliver to the office"   -> remember
  "what do you remember about me?"    -> list
  "forget that I'm vegetarian"        -> forget the closest match (only if it is clearly the same memory)
  "forget everything"                 -> forget all of this user's memories
"""
import re
from dataclasses import dataclass
from typing import Literal

MAX_MEMORY_CHARS = 500

_REMEMBER = re.compile(r"^\s*(?:please\s+)?(?:remember|note|keep in mind)(?:\s+that)?\s*[:,-]?\s+(?P<body>.+?)\s*$",
                       re.IGNORECASE | re.DOTALL)
_LIST = re.compile(r"^\s*(?:what\s+do\s+you\s+(?:remember|know)(?:\s+about\s+me)?|show\s+(?:me\s+)?my\s+memor(?:y|ies)|list\s+(?:my\s+)?memor(?:y|ies))\s*\??\s*$",
                   re.IGNORECASE)
_FORGET_ALL = re.compile(r"^\s*(?:please\s+)?forget\s+(?:everything|all(?:\s+of\s+it)?|all\s+my\s+memories)(?:\s+about\s+me)?\s*[.!]?\s*$",
                         re.IGNORECASE)
_FORGET = re.compile(r"^\s*(?:please\s+)?forget(?:\s+that)?\s*[:,-]?\s+(?P<body>.+?)\s*[.!]?\s*$", re.IGNORECASE | re.DOTALL)
# "remember to book a table" is a task, not a fact.
_TASKY = re.compile(r"^\s*to\s+\w+", re.IGNORECASE)


@dataclass(frozen=True)
class MemoryCommand:
    action: Literal["remember", "list", "forget", "forget_all"]
    text: str = ""


def parse_memory_command(message: str) -> MemoryCommand | None:
    """Return a command if the whole message is a memory instruction, else None (it goes to the agent)."""
    if not message or len(message) > MAX_MEMORY_CHARS + 40:
        return None
    if _LIST.match(message):
        return MemoryCommand("list")
    if _FORGET_ALL.match(message):
        return MemoryCommand("forget_all")
    if m := _FORGET.match(message):
        return MemoryCommand("forget", _clean(m["body"]))
    if m := _REMEMBER.match(message):
        body = _clean(m["body"])
        if body and not _TASKY.match(body):
            return MemoryCommand("remember", body[:MAX_MEMORY_CHARS])
    return None


FORGET_MIN_SIMILARITY = 0.8
LIST_LIMIT = 20


async def run_memory_command(ltm, cmd: MemoryCommand, *, org_id: str, user_id: str, thread_id: str | None) -> str:
    """Carry out a memory command for this user and return the reply. Only user-scoped memories are written or deleted."""
    from app.memory.long_term import SecretInMemoryError

    if cmd.action == "remember":
        try:
            await ltm.remember(org_id=org_id, scope="user", user_id=user_id, content=cmd.text, kind="preference",
                               importance=0.7, source_thread_id=thread_id, created_by=user_id)
        except SecretInMemoryError:
            return "That looks like a secret, so I didn't save it to memory. Use **Add a login or API key** instead."
        return f"Got it. I'll remember: “{cmd.text}”. Say “forget that {cmd.text}” to remove it."

    if cmd.action == "list":
        items = await ltm.list_visible(org_id=org_id, user_id=user_id)
        if not items:
            return "I don't have anything saved about you yet. Say “remember that …” to add something."
        lines = [f"- {i.content}" + ("" if i.scope == "user" else " *(shared by your organisation)*") for i in items[:LIST_LIMIT]]
        more = f"\n…and {len(items) - LIST_LIMIT} more." if len(items) > LIST_LIMIT else ""
        return "Here's what I remember:\n" + "\n".join(lines) + more

    if cmd.action == "forget_all":
        n = await ltm.forget_user(org_id=org_id, user_id=user_id)
        return f"Done. I deleted {n} saved memor{'y' if n == 1 else 'ies'} about you." if n else "There was nothing saved to forget."

    if cmd.action == "forget":
        found = await ltm.recall(org_id=org_id, user_id=user_id, query=cmd.text, k=3)
        mine = [m for m in found if m.scope == "user" and m.similarity >= FORGET_MIN_SIMILARITY]
        if not mine:
            return ("I couldn't find a saved memory matching that, so nothing was deleted. "
                    "Say “what do you remember?” to see the exact wording.")
        target = max(mine, key=lambda m: m.similarity)
        await ltm.forget(memory_id=target.id, actor_id=user_id)
        return f"Forgotten: “{target.content}”."

    raise ValueError(cmd.action)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().rstrip(".!")


def memory_block(memories: list[str]) -> str:
    """How recalled memories are shown to a model: the user's own past words, as data. Rules still win."""
    if not memories:
        return ""
    lines = "\n".join(f"- {m}" for m in memories)
    return ("Things this user asked me to remember earlier (their own words; may be out of date; use them only if relevant; "
            "firm and user rules take precedence):\n" + lines)
