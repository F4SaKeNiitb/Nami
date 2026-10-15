"""Detect secret-shaped text so it never lands in memory, caches or model-facing transcripts.

The detector deliberately errs toward false positives: a wrongly redacted word is cheap,
while a leaked key is not. The vault's chat prefilter uses the same patterns.
"""

import re
from dataclasses import dataclass

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("api_key", re.compile(r"\bsk-(?:or-v1-|ant-[a-z0-9]+-|proj-)?[A-Za-z0-9_\-]{20,}")),
    ("api_key", re.compile(r"\b(?:ghp|gho|ghs|ghu|github_pat)_[A-Za-z0-9_]{20,}")),
    ("api_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("api_key", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("api_key", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("totp_uri", re.compile(r"otpauth://\S+")),
    ("labeled_secret", re.compile(
        r"(?i)\b(?:password|passwd|pwd|passcode|pin|secret|api[_ ]?key|token|cvv|cvc|otp)\b"
        r"\s*(?:is|=|:)\s*\S+")),
    # Base32 TOTP seeds, contiguous or in groups of 4. Requiring a digit 2-7 keeps ordinary
    # upper-case words from matching.
    ("totp_seed", re.compile(r"\b(?=[A-Z2-7]*[2-7])[A-Z2-7]{16,64}\b")),
    ("totp_seed", re.compile(r"\b(?=[A-Z2-7 ]*[2-7])(?:[A-Z2-7]{4} ){3,15}[A-Z2-7]{4}\b")),
]
_CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")


@dataclass(frozen=True)
class Finding:
    kind: str
    start: int
    end: int


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


_CARDISH_KEY = re.compile(r"(?i)card|\bcc|pan\b|cvv|cvc|pay|number|\bnum")


def _url_id_not_card(text: str, m: re.Match) -> bool:
    """A long digit run inside a URL (order id, tracking id) that happens to pass the Luhn check is not a card, unless
    the parameter or path segment in front of it is named like one (card, cc, pan, pay, number). Outside URLs this
    never applies, so a card typed in prose is always caught."""
    start = max(text.rfind(" ", 0, m.start()), text.rfind("\n", 0, m.start()), text.rfind("\t", 0, m.start())) + 1
    prefix = text[start:m.start()]
    if "://" not in prefix and not prefix.startswith("/") and "?" not in prefix:
        return False
    if re.search(r"\d[ -]\d", m.group()):          # grouped like a typed card number: not an id
        return False
    key = re.split(r"[?&/;]", prefix)[-1]
    return not _CARDISH_KEY.search(key)


def find_secrets(text: str) -> list[Finding]:
    found = [Finding(kind, m.start(), m.end()) for kind, rx in _PATTERNS for m in rx.finditer(text)]
    for m in _CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and _luhn_ok(digits) and not _url_id_not_card(text, m):
            found.append(Finding("card_number", m.start(), m.end()))
    return sorted(found, key=lambda f: (f.start, -f.end))


def contains_secret(text: str) -> bool:
    return bool(find_secrets(text))


def redact(text: str) -> str:
    out, cursor = [], 0
    for f in find_secrets(text):
        if f.start < cursor:  # overlaps a span already redacted
            continue
        out.append(text[cursor:f.start])
        out.append(f"[REDACTED:{f.kind}]")
        cursor = f.end
    out.append(text[cursor:])
    return "".join(out)
