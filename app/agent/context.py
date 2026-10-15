"""What the browser agent is shown, and what it may do, for one subgoal. Plain code, no model calls.

  * `compact_notes`: earlier verified steps and the user's answers become a bounded digest. The newest steps stay whole, older
    ones shrink to their first sentence, and the whole block has a hard size cap, so a long task cannot grow the prompt.
  * `masked_actions`: the browser-use actions a subgoal does not need are not offered to the model. This is a convenience and
    a second line behind the Guard, never the safety boundary: the Guard, the domain allow-list and the placeholders hold
    whatever the model calls.
"""

import re

from app.agent.schemas import AccessPlan, Subgoal

NOTES_MAX_CHARS = 1200
RECENT_KEEP = 3
RECENT_CHARS = 300
OLDER_CHARS = 80
ANSWERS_KEEP = 2
ANSWER_CHARS = 300

_SENTENCE = re.compile(r"(.+?[.!?])(\s|$)", re.S)


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _first_sentence(text: str, limit: int) -> str:
    t = _one_line(text)
    m = _SENTENCE.match(t)
    return (m.group(1) if m else t)[:limit]


def compact_notes(progress: list[str] | None, *, keep: int = RECENT_KEEP, max_chars: int = NOTES_MAX_CHARS) -> str:
    """One bounded string for the prompt. Empty input gives ''. Repeats are dropped; the newest `keep` notes stay (cut to
    RECENT_CHARS), the rest become a one-line-each digest of their first sentence."""
    seen: list[str] = []
    for p in progress or []:
        p = _one_line(p)
        if p and p not in seen:
            seen.append(p)
    if not seen:
        return ""
    older, recent = seen[:-keep] if keep else seen, seen[-keep:] if keep else []
    parts: list[str] = []
    if older:
        parts.append(f"{len(older)} earlier step(s): " + "; ".join(_first_sentence(o, OLDER_CHARS) for o in older))
    parts += [r[:RECENT_CHARS] for r in recent]
    out = " | ".join(parts)
    if len(out) <= max_chars:
        return out
    # Over the cap: keep the tail (newest) because that is what the next step continues from.
    return "…" + out[-(max_chars - 1):]


def compact_answers(answers: list[str] | None) -> str:
    """The user's answers to the agent's own questions (newest few), as data."""
    kept = [_one_line(a)[:ANSWER_CHARS] for a in (answers or []) if _one_line(a)][-ANSWERS_KEEP:]
    return " | ".join(kept)


# Pages and screenshots are read through these; a sign-in step has no reason to dump a page.
_READING = ["extract", "read_long_content", "search_page"]      # (screenshot is excluded for every step, see executor.EXCLUDED_ACTIONS)
# Form widgets that a pure lookup never needs. `input` and `send_keys` stay: searching a public site is still read-only.
_FORM_WIDGETS = ["dropdown_options", "select_dropdown"]


def is_read_only(subgoal: Subgoal, access: AccessPlan) -> bool:
    """A lookup on a public site: nothing to sign in to, nothing to pay or approve, nothing irreversible."""
    return (not subgoal.irreversible and not subgoal.is_auth and access.login == "none" and not access.credentials
            and not access.cards and not access.approval_actions)


def masked_actions(subgoal: Subgoal, access: AccessPlan) -> list[str]:
    """Extra actions to hide from the model for this subgoal, on top of the executor's permanent exclusions."""
    if subgoal.is_auth:
        return list(_READING)
    if is_read_only(subgoal, access):
        return list(_FORM_WIDGETS)
    return []


_SECRET_WORDS = re.compile(r"\b(pass(?:word|code|wd)|otp|one[- ]time|verification code|2fa|cvv|cvc|card number|card details|"
                           r"pin|ssn|secret|api[- ]?key|token|seed)\b", re.I)


def asks_for_secret(question: str) -> bool:
    """True if a question the agent wants to put to the user is really a request for a credential. Those never go through chat."""
    return bool(_SECRET_WORDS.search(question or ""))
