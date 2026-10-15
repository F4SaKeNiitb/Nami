"""How an API wants its key. One small closed set of ways to send a key; the language model may only choose among them.

The model never sees the key and never chooses where it goes: the host is the registry's API host (and the credential's origin
binding still applies in the vault). What it may say is the *shape* of the credential:

  bearer   Authorization: Bearer <key>
  header   <Name>: [<Prefix> ]<key>          (x-api-key, api-key, Authorization: Token <key> ...)
  basic    Authorization: Basic base64(<key>:)
  query    ?<name>=<key>

plus optional static headers an API requires next to the key (a version header). Every field is validated against a strict
pattern here, so a model (or a poisoned doc page it read) cannot smuggle in a header like Host or Cookie, a second credential,
or anything that is not one of these four shapes.
"""

import base64
import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

KINDS = ("bearer", "header", "basic", "query")
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,39}")
_PREFIX = re.compile(r"[A-Za-z]{1,20} ")
_STATIC_VALUE = re.compile(r"[A-Za-z0-9._;=/ +-]{1,80}")
_FORBIDDEN = {"host", "cookie", "set-cookie", "content-length", "transfer-encoding", "connection", "proxy-authorization",
              "origin", "referer", "te", "upgrade", "x-forwarded-for", "x-forwarded-host", "x-real-ip", "authorization"}
MAX_EXTRA = 3


class SchemeError(ValueError):
    """A scheme that is not one of the allowed shapes."""


@dataclass(frozen=True)
class Scheme:
    kind: str
    name: str = ""                    # header or query parameter name (header / query)
    prefix: str = ""                  # "Token " in "Authorization: Token <key>" (header only)
    extra: tuple[tuple[str, str], ...] = field(default_factory=tuple)      # static headers that are not secret

    def to_dict(self) -> dict:
        d: dict = {"kind": self.kind}
        if self.name:
            d["name"] = self.name
        if self.prefix:
            d["prefix"] = self.prefix
        if self.extra:
            d["extra_headers"] = dict(self.extra)
        return d

    def describe(self) -> str:
        """For the user's approval card. Never contains the key."""
        base = {"bearer": "as a bearer token (Authorization header)",
                "header": f"in the '{self.name}' header" + (f" after '{self.prefix.strip()}'" if self.prefix else ""),
                "basic": "as the user name of HTTP basic auth",
                "query": f"in the '{self.name}' URL parameter"}[self.kind]
        return base + (f", with the fixed header{'s' if len(self.extra) > 1 else ''} {', '.join(k for k, _ in self.extra)}" if self.extra else "")

    def apply(self, key: str) -> tuple[dict[str, str], dict[str, str]]:
        """(headers, query params) for one request carrying this key."""
        headers = {"Accept": "application/json", **dict(self.extra)}
        params: dict[str, str] = {}
        if self.kind == "bearer":
            headers["Authorization"] = f"Bearer {key}"
        elif self.kind == "header":
            headers[self.name] = f"{self.prefix}{key}"
        elif self.kind == "basic":
            headers["Authorization"] = "Basic " + base64.b64encode(f"{key}:".encode()).decode()
        else:
            params[self.name] = key
        return headers, params


def parse_scheme(raw) -> Scheme:
    """Build a Scheme from a registry value, a remembered value or a model's proposal; SchemeError if it is not allowed."""
    if isinstance(raw, str):
        raw = {"kind": raw.strip().lower()}
    if not isinstance(raw, dict):
        raise SchemeError("not a scheme")
    kind = str(raw.get("kind", "")).strip().lower()
    if kind not in KINDS:
        raise SchemeError(f"unknown kind {kind!r}")
    name, prefix = str(raw.get("name") or "").strip(), str(raw.get("prefix") or "")
    if prefix and not prefix.endswith(" "):
        prefix += " "
    if kind in ("header", "query"):
        if not _NAME.fullmatch(name):
            raise SchemeError("bad name")
        if kind == "header" and name.lower() in _FORBIDDEN - {"authorization"}:
            raise SchemeError("that header is not allowed")
    elif name:
        raise SchemeError("name is only for header and query")
    if prefix and (kind != "header" or not _PREFIX.fullmatch(prefix)):
        raise SchemeError("bad prefix")
    extra_raw = raw.get("extra_headers") or {}
    if not isinstance(extra_raw, dict) or len(extra_raw) > MAX_EXTRA:
        raise SchemeError("too many extra headers")
    extra = []
    for k, v in extra_raw.items():
        k, v = str(k).strip(), str(v).strip()
        if not _NAME.fullmatch(k) or k.lower() in _FORBIDDEN or k.lower() == name.lower() or not _STATIC_VALUE.fullmatch(v):
            raise SchemeError("bad extra header")
        extra.append((k, v))
    return Scheme(kind=kind, name=name, prefix=prefix, extra=tuple(sorted(extra)))


def try_parse(raw) -> Scheme | None:
    try:
        return parse_scheme(raw)
    except SchemeError:
        return None


# ───────────── asking the model (it sees the site and its public endpoint summaries; never the key) ─────────────

class SchemeGuess(BaseModel):
    kind: Literal["bearer", "header", "basic", "query"]
    name: str = Field(default="", description="header or query parameter name; empty for bearer and basic")
    prefix: str = Field(default="", description="word before the key in the header value, e.g. 'Token'; usually empty")
    extra_headers: dict[str, str] = Field(default_factory=dict, description="fixed non-secret headers the API requires, e.g. a version")


class SchemeProposal(BaseModel):
    schemes: list[SchemeGuess] = Field(default_factory=list, max_length=3, description="most likely first, at most 3")


SYSTEM = """You know how web APIs take an API key. Say how the API below expects it, most likely first, at most 3 options.
Allowed shapes only: bearer (Authorization: Bearer <key>); header (a named header such as x-api-key or api-key, optional word
prefix such as Token); basic (key as the user name of HTTP Basic auth); query (a URL parameter such as key or api_key).
If the API needs a fixed, non-secret header next to the key (for example a version header), put it in extra_headers.
You will never see the key. Do not invent hosts or URLs; only the shape of the credential is yours to choose."""

# Tried after the model's guesses when it is unavailable or wrong. Cheap, common, and each is just another way to send the same key.
DEFAULTS = (Scheme("bearer"), Scheme("header", name="x-api-key"), Scheme("header", name="api-key"), Scheme("basic"))
MAX_TRIES = 4


async def propose(llm, *, site: str, host: str, endpoints: dict, model: str | None = None) -> list[Scheme]:
    """Up to MAX_TRIES distinct, validated candidates: the model's guesses first, then the common defaults."""
    menu = "\n".join(f"- {n}: {spec.get('summary', '')}" for n, spec in list(endpoints.items())[:8])
    guesses: list[Scheme] = []
    try:
        out = await llm.json(system=SYSTEM, user=f"Service: {site}\nAPI host: {host}\nWhat it offers:\n{menu}",
                             schema=SchemeProposal, model=model, max_tokens=600)
        guesses = [s for s in (try_parse(g.model_dump()) for g in out.schemes) if s]
    except Exception:                                  # model down or unreadable reply: the defaults still give it a fair try
        guesses = []
    seen, ordered = set(), []
    for s in [*guesses, *DEFAULTS]:
        if s not in seen:
            seen.add(s)
            ordered.append(s)
    return ordered[:MAX_TRIES]


def from_registry(api: dict | None) -> Scheme | None:
    """The scheme an API block declares. A missing value means bearer; 'auto' means: work it out (None)."""
    if not api:
        return None
    raw = api.get("auth", "bearer")
    if isinstance(raw, str) and raw.strip().lower() == "auto":
        return None
    return parse_scheme(raw)


def is_auto(api: dict | None) -> bool:
    return bool(api) and isinstance(api.get("auth"), str) and api["auth"].strip().lower() == "auto"

