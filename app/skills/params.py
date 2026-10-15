"""Parameterised workflow steps (a light form of Agent Workflow Memory).

A learned workflow that says "filter to under ₹300" is only useful for the next request if the amount can change. When a
skill is saved, any constraint the user gave that appears word for word in a step is replaced by a named slot
(`{constraint_1}`), and the original wording is kept as the example. When the skill is used for a new request, each slot
is filled from THAT request's constraints (user text), or from a neutral phrase if the new request gave none.

Pure and deterministic: no model, nothing read from a page. Only the user's own constraint text is ever substituted.
"""

import re

MAX_PARAMS = 5
_SLOT = re.compile(r"\{constraint_(\d+)\}")
NEUTRAL = "what the user asked for"


def parameterise(steps: list[str], constraints: list[str]) -> tuple[list[str], dict[str, str]]:
    params: dict[str, str] = {}
    out = list(steps)
    for i, raw in enumerate(constraints[:MAX_PARAMS], 1):
        c = (raw or "").strip()
        if not 3 <= len(c) <= 60 or "{" in c:
            continue
        rx = re.compile(re.escape(c), re.I)
        if any(rx.search(s) for s in out):
            out = [rx.sub(f"{{constraint_{i}}}", s) for s in out]
            params[f"constraint_{i}"] = c
    return out, params


def fill(text: str, params: dict[str, str], constraints: list[str]) -> str:
    """Replace every slot in `text` with the matching constraint of the current request, never with the saved example."""
    def pick(m: re.Match) -> str:
        i = int(m.group(1)) - 1
        v = (constraints[i].strip() if 0 <= i < len(constraints) else "")
        return v[:80] if v else NEUTRAL
    return _SLOT.sub(pick, text)
