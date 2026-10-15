"""Deterministic scanner for skills. Blocking rules only; the judge (judge.py) can lower trust but never raise it.

Every field of the bundle is scanned, including ones a UI never shows. A skill that fails is rejected, not edited.
"""

import re
import unicodedata
from dataclasses import dataclass, field

from app.security.gateway import _INJECTION, _INVISIBLE
from app.security.secrets import find_secrets
from app.skills.schemas import Manifest, SkillContent
from app.vault.origins import registrable_domain

_URL = re.compile(r"https?://[^\s)\"'>]+", re.I)
_PLACEHOLDER = re.compile(r"<\s*/?\s*secret\s*>|\bcred_[0-9a-f]{6,}_\w+|<<\s*cred_\w+\s*>>", re.I)
_FETCH_PHRASE = re.compile(r"\b(fetch|download|open|follow|read|load|visit|go to|navigate to)\b.{0,40}\b(then|and)\b.{0,30}\b(apply|follow|obey|run|execute|use)\b", re.I)
_PRIVILEGE = re.compile(
    r"\b(first install|prerequisite|before (?:you )?(?:start|begin|continue).{0,30}(?:install|run|enable)|"
    r"disable (?:the )?(?:approval|confirmation|guard|safety)|skip (?:the )?(?:approval|confirmation|check)|"
    r"without asking|do not ask the user|write (?:to )?(?:memory|identity)|remember this (?:rule|instruction)|"
    r"mark (?:this|it) (?:as )?safe|trusted skill|paste .{0,20}(?:password|key|token|otp|card)|send .{0,20}(?:password|key|token|otp|card))\b", re.I)
_CODE_DENY = re.compile(r"\b(fetch|XMLHttpRequest|WebSocket|EventSource|sendBeacon|eval|Function|importScripts|import)\s*\(|"
                        r"\bnew\s+(?:Function|WebSocket|Worker)\b|document\.cookie|localStorage|sessionStorage|indexedDB|"
                        r"window\.open|location\s*(?:=|\.href|\.assign|\.replace)|\.src\s*=|navigator\.", re.I)
MAX_CODE_LINES = 40


@dataclass(frozen=True)
class Finding:
    rule: str
    detail: str


@dataclass
class ScanResult:
    findings: list[Finding] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.findings

    def to_json(self) -> dict:
        return {"passed": self.passed, "findings": [{"rule": f.rule, "detail": f.detail} for f in self.findings]}


def _mixed_script(word: str) -> bool:
    scripts = set()
    for ch in word:
        if ch.isalpha():
            scripts.add(unicodedata.name(ch, "?").split()[0])
    return len(scripts & {"LATIN", "CYRILLIC", "GREEK", "ARMENIAN"}) > 1


def strip_placeholders(text: str) -> tuple[str, bool]:
    new = _PLACEHOLDER.sub("[removed]", text)
    return new, new != text


def scan(content: SkillContent, manifest: Manifest) -> ScanResult:
    r = ScanResult()
    add = lambda rule, detail: r.findings.append(Finding(rule, detail[:80]))  # noqa: E731
    allowed = set(manifest.domains)
    for text in content.texts():
        if _INVISIBLE.search(text) or unicodedata.normalize("NFKC", text) != text:
            add("unicode", "invisible, bidi or look-alike characters")
        if any(_mixed_script(w) for w in re.findall(r"\w+", text)):
            add("unicode", "word mixes scripts (look-alike letters)")
        if find_secrets(text):
            add("secret", "secret-shaped text")
        if _PLACEHOLDER.search(text):
            add("placeholder", "credential placeholder inside a skill")
        for m in _URL.finditer(text):
            d = registrable_domain(m.group())
            if d not in allowed:
                add("off_manifest_url", m.group())
        for name, rx in _INJECTION:
            if rx.search(text):
                add("injection", name)
        if _FETCH_PHRASE.search(text):
            add("external_instruction", "tells the agent to fetch something and then apply it")
        if _PRIVILEGE.search(text):
            add("privilege", "asks for approval bypass, prerequisites, memory writes or secret handling")
    if content.site not in allowed:
        add("manifest", "skill site is not in the manifest domains")
    if content.code is not None:
        if not manifest.has_code:
            add("manifest", "code present but manifest.has_code is false")
        if _CODE_DENY.search(content.code):
            add("code_denylist", "network, eval, storage, cookie or navigation API")
        if content.code.count("\n") + 1 > MAX_CODE_LINES:
            add("code_size", f"more than {MAX_CODE_LINES} lines")
    elif manifest.has_code:
        add("manifest", "manifest says code but there is none")
    return r
