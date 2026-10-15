"""Approval policy for registry API endpoints, derived from the HTTP method.

A registry label can only RAISE the risk of an endpoint, never lower it: a `kind: read` endpoint that is declared as
POST still needs approval, and a `kind: generate` endpoint needs approval whatever its method says. Anything we do not
recognise is treated as a change. The graph, `ApiExecutor.call` and the registry lint all use this one function, so a
new endpoint is safe by default without anyone remembering to mark it.
"""

from typing import Literal

Risk = Literal["read", "write", "destructive"]

_SAFE = {"GET", "HEAD"}
_DESTRUCTIVE = {"DELETE"}
_ORDER = {"read": 0, "write": 1, "destructive": 2}


def method_of(spec: dict) -> str:
    return str(spec.get("method") or "GET").upper()


def risk(spec: dict) -> Risk:
    """read: runs without asking. write: changes something or spends credits. destructive: removes something."""
    m = method_of(spec)
    by_method: Risk = "read" if m in _SAFE else "destructive" if m in _DESTRUCTIVE else "write"
    by_label: Risk = "write" if spec.get("kind", "read") != "read" else "read"
    return max(by_method, by_label, key=_ORDER.__getitem__)


def needs_approval(spec: dict) -> bool:
    return risk(spec) != "read"


def lint(sites: dict) -> list[str]:
    """Problems in the registry's endpoint declarations (empty when it is consistent). Used by a test, not at runtime."""
    out: list[str] = []
    for site, d in sites.items():
        for name, spec in ((d.get("api") or {}).get("endpoints") or {}).items():
            where = f"{site}.{name}"
            kind = spec.get("kind", "read")
            if kind not in ("read", "generate"):
                out.append(f"{where}: unknown kind {kind!r}")
            if kind == "read" and method_of(spec) not in _SAFE:
                out.append(f"{where}: labelled read but uses {method_of(spec)}")
            if kind == "generate" and method_of(spec) != "POST":
                out.append(f"{where}: generate endpoints must be POST")
            if kind == "generate" and "body" not in spec:
                out.append(f"{where}: generate endpoint has no body template")
    return out
