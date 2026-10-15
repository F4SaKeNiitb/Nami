"""Prompt-injection defence for text that reaches OUR model calls (validator, verifier).

Layers here are deterministic:
  L1  provenance: untrusted text is wrapped and cannot close its own wrapper
  L2  sanitiser: invisible characters, HTML comments, control characters, length caps
  L2b heuristic scan for instruction-like text; hits are quarantined (removed) and reported
  L5  egress: secret-shaped text may not leave, and URLs must be inside the access plan

Not covered: the page text that browser-use feeds to the executor model itself. That path has no hook
in browser-use 0.11, so the executor is bounded by the access plan, allowed_domains, placeholders and
the approval gate instead. A model-based classifier (layer 3) is not built.
"""

import re
import unicodedata
from dataclasses import dataclass, field

from app.security.secrets import contains_secret
from app.vault.origins import registrable_domain

MAX_LEN = 6000
_INVISIBLE = re.compile("[​‌‍⁠﻿‪-‮⁦-⁩­]")
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_INJECTION: list[tuple[str, re.Pattern[str]]] = [
    ("override_instructions", re.compile(r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|earlier|all|your)\b.{0,30}\b(instructions?|rules?|prompts?)\b", re.I)),
    ("role_hijack", re.compile(r"\b(you are now|from now on you|act as|pretend to be|new instructions?|system prompt)\b", re.I)),
    ("secrecy", re.compile(r"\b(do not|don't|never)\b.{0,25}\b(tell|inform|show|mention)\b.{0,20}\b(the )?user\b", re.I)),
    ("credential_request", re.compile(r"\b(enter|type|send|reveal|share|paste|submit)\b.{0,40}\b(password|passcode|api[ _-]?key|secret|token|otp|cvv|card number)\b", re.I)),
    ("exfiltrate", re.compile(r"\b(send|post|forward|upload|email)\b.{0,50}\bto\b.{0,20}(https?://|[\w.-]+@[\w.-]+)", re.I)),
    ("markdown_image_exfil", re.compile(r"!\[[^\]]*\]\(https?://[^)\s]*[?&][^)\s]*=", re.I)),
    ("fake_boundary", re.compile(r"</?\s*(untrusted_page|system|assistant|tool|secret)\b[^>]*>", re.I)),
    ("tool_call_text", re.compile(r"\b(call|invoke|run|use)\b.{0,20}\b(the )?(tool|function|action)\b.{0,30}\b(request_help|confirm|approve|navigate|go_to_url)\b", re.I)),
]


@dataclass(frozen=True)
class Hit:
    layer: str
    rule: str
    excerpt: str


@dataclass
class Verdict:
    text: str                                   # safe to give to a model
    hits: list[Hit] = field(default_factory=list)
    removed_invisible: int = 0

    @property
    def suspicious(self) -> bool:
        return bool(self.hits)


def sanitize(text: str, max_len: int = MAX_LEN) -> tuple[str, int]:
    removed = len(_INVISIBLE.findall(text))
    text = unicodedata.normalize("NFKC", text)     # folds look-alike forms of the characters below
    text = _INVISIBLE.sub("", text)
    text = _COMMENT.sub("", text)
    text = _CONTROL.sub("", text)
    return text[:max_len], removed


def inspect_page_text(text: str, *, max_len: int = MAX_LEN, ignore: frozenset[str] = frozenset()) -> Verdict:
    """Sanitise, then quarantine any line that reads like an instruction aimed at the agent. `ignore` names rules to skip: on a sign-in step the
    page asking for a password or a code is its job, so \"credential_request\" is skipped there (typing is still limited by origin, by the
    placeholder guard and by the capability checks, so a page cannot talk the agent into typing a secret elsewhere)."""
    clean, removed = sanitize(text, max_len)
    hits: list[Hit] = []
    kept: list[str] = []
    for line in clean.splitlines():
        bad = [(name, rx) for name, rx in _INJECTION if name not in ignore and rx.search(line)]
        if bad:
            hits += [Hit("L2", name, line.strip()[:80]) for name, _ in bad]
            kept.append("[removed: text on the page that tried to give instructions]")
        else:
            kept.append(line)
    return Verdict("\n".join(kept), hits, removed)


def wrap_untrusted(text: str, label: str = "untrusted_page") -> str:
    """Wrap page-derived text so a model treats it as data. The page cannot close the wrapper."""
    safe = re.sub(rf"</?\s*{label}\b[^>]*>", "[tag removed]", text, flags=re.I)
    return f"<{label}>\n{safe}\n</{label}>"


def url_in_plan(url: str, allowed_domains: list[str]) -> bool:
    d = registrable_domain(url)
    return bool(d) and d in allowed_domains


def egress_blocked(text: str) -> bool:
    """True if text about to be typed, posted or sent contains something secret-shaped."""
    return contains_secret(text)
