"""Claude client for browser-use that works with models which reject a forced tool_choice.

browser-use 0.11 forces `tool_choice={"type": "tool", ...}` for structured output. Claude 5 models answer that with a 400
("tool_choice: type 'tool' and 'any' are not supported for this model"). browser-use 0.13 fixes it upstream, but 0.13 pins
pyotp==2.9.0 and about fifteen other exact versions, so we stay on 0.11 and downgrade the parameter to "auto" here.
The request still offers exactly one tool, so the model's only way to answer is to call it.
"""

import json
import logging
import os

# Set before browser_use is imported: no PyPI version check on every run, and no usage telemetry sent to a third party
# from a product that handles credentials.
os.environ.setdefault("BROWSER_USE_VERSION_CHECK", "false")
os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")

from anthropic.types import ToolUseBlock  # noqa: E402
from browser_use.llm import ChatAnthropic  # noqa: E402

log = logging.getLogger("nami.browser_llm")
EXTRA_ATTEMPTS = 2          # when a relaxed call comes back with prose instead of the tool call

_FORCED = ("tool", "any")
NUDGE = (
    "\n\nOUTPUT FORMAT: always reply by calling the provided tool exactly once. Its input is ALWAYS an object with the keys "
    "thinking, evaluation_previous_goal, memory, next_goal and action, where action is a list of single-key objects. "
    "Never put an action's own arguments (query, selector, index, url, code...) at the top level. Correct example: "
    '{"thinking": "...", "evaluation_previous_goal": "...", "memory": "...", "next_goal": "...", '
    '"action": [{"extract": {"query": "the heading", "extract_links": true}}]}'
)


def relax_tool_choice(kwargs: dict) -> dict:
    tc = kwargs.get("tool_choice")
    if isinstance(tc, dict) and tc.get("type") in _FORCED:
        system = kwargs.get("system")
        if isinstance(system, str):                      # without a forced choice, say it in words
            kwargs = {**kwargs, "system": system + NUDGE}
        elif isinstance(system, list):
            kwargs = {**kwargs, "system": [*system, {"type": "text", "text": NUDGE.strip()}]}
        return {**kwargs, "tool_choice": {"type": "auto"}}
    return kwargs


MAX_COMPLETED_BRACKETS = 3
MAX_SCAN_CHARS = 200_000


def _missing_closers(fragment: str) -> str | None:
    """The closing brackets a JSON fragment is missing, or None if it is unbalanced the wrong way or was cut inside a string
    (then we do not guess)."""
    stack: list[str] = []
    in_str = esc = False
    for ch in fragment:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack or stack.pop() != ch:
                return None
    return None if in_str else "".join(reversed(stack))


def _salvage(text: str):
    """The model sometimes puts the tool input in prose, a code fence, or inside another field. Find the first JSON object in
    the text that parses and carries an `action` list (it may be preceded by reasoning that itself contains braces)."""
    decoder = json.JSONDecoder()
    tried = 0
    text = text[:MAX_SCAN_CHARS]
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        tried += 1
        if tried > 60:                                  # bounded work on a long reply
            break
        try:
            data, _ = decoder.raw_decode(text[i:])
        except (ValueError, RecursionError):       # RecursionError: absurdly nested text must not break the model call
            data = _complete(text[i:])             # seen live: the answer's last "}" is missing
        if isinstance(data, dict) and isinstance(data.get("action"), list):
            return data
    return None


def _complete(fragment: str):
    if '"action"' not in fragment:
        return None
    tail = fragment.rstrip().rstrip("`").rstrip()
    closers = _missing_closers(tail)
    if not closers or len(closers) > MAX_COMPLETED_BRACKETS:
        return None
    try:
        return json.loads(tail + closers)
    except (ValueError, RecursionError):
        return None


_META = ("thinking", "evaluation_previous_goal", "memory", "next_goal", "current_plan_item", "plan_update")


def _nullable(sub: dict) -> bool:
    return any(isinstance(o, dict) and o.get("type") == "null" for o in sub.get("anyOf") or []) or sub.get("type") == "null"


def _null_strings(schema: dict, data):
    """The model writes the string "null" for an empty optional argument; turn it into None where the schema allows null."""
    try:
        variants = schema["properties"]["action"]["items"]["anyOf"]
    except (KeyError, TypeError):
        return data
    allowed = {n: (sub or {}).get("properties") or {} for v in variants for n, sub in (v.get("properties") or {}).items()}
    actions = data.get("action") if isinstance(data, dict) else None
    if not isinstance(actions, list):
        return data
    fixed = []
    for item in actions:
        if isinstance(item, dict) and len(item) == 1:
            (name, args), = item.items()
            if isinstance(args, dict) and name in allowed:
                args = {k: (None if v == "null" and _nullable(allowed[name].get(k) or {}) else v) for k, v in args.items()}
            item = {name: args}
        fixed.append(item)
    return {**data, "action": fixed}


def repair_tool_input(schema: dict, data):
    """Without a forced tool_choice the model sometimes sends ONE action's arguments at the top level, e.g.
    {"query": ..., "extract_links": "true"}. If exactly one action in the schema accepts those keys, wrap it as
    {"action": [{name: args}]}. Anything ambiguous is returned unchanged and fails validation as it would have anyway.
    The repaired call is still validated, domain-limited and guarded like any other."""
    if not isinstance(data, dict):
        return data
    if "action" in data:
        return _null_strings(schema, data)
    for value in data.values():                         # the whole answer written inside one string field (often "thinking")
        if isinstance(value, str) and '"action"' in value:
            found = _salvage(value)
            if found is not None:
                for k in ("thinking", "evaluation_previous_goal", "memory", "next_goal"):
                    found.setdefault(k, "")
                return _null_strings(schema, found)
    try:
        variants = schema["properties"]["action"]["items"]["anyOf"]
    except (KeyError, TypeError):
        return data
    args = {k: v for k, v in data.items() if k not in _META}
    if not args:
        return data
    best: list[str] = []
    for v in variants:
        for name, sub in (v.get("properties") or {}).items():
            props = set((sub or {}).get("properties") or {})
            if set(args) <= props and set((sub or {}).get("required") or []) <= set(args) | {k for k in props if k not in args}:
                best.append(name)
    if len(best) != 1:
        return data
    meta = {k: data[k] for k in _META if k in data}
    for k in ("thinking", "evaluation_previous_goal", "memory", "next_goal"):
        meta.setdefault(k, "")
    return _null_strings(schema, {**meta, "action": [{best[0]: args}]})


class AutoToolChoiceAnthropic(ChatAnthropic):
    def get_client(self):
        client = super().get_client()
        messages = client.messages
        if not getattr(messages, "_nami_relaxed", False):
            original = messages.create

            async def create(*args, **kwargs):
                relaxed = relax_tool_choice(kwargs)
                response = await original(*args, **relaxed)
                if relaxed is kwargs:
                    return response
                tool = (relaxed.get("tools") or [{}])[0]
                schema = tool.get("input_schema") or {}
                for attempt in range(EXTRA_ATTEMPTS + 1):    # we relaxed this call: insist on a tool call, then repair it
                    blocks = list(getattr(response, "content", []))
                    if any(getattr(b, "type", "") == "tool_use" for b in blocks):
                        for b in blocks:
                            if getattr(b, "type", "") == "tool_use":
                                b.input = repair_tool_input(schema, b.input)
                        return response
                    text = " ".join(getattr(b, "text", "") for b in blocks if getattr(b, "type", "") == "text")
                    salvaged = _salvage(text)
                    if salvaged is not None and tool.get("name"):
                        response.content = [ToolUseBlock(id="toolu_nami_salvaged", type="tool_use", name=tool["name"],
                                                         input=repair_tool_input(schema, salvaged))]
                        return response
                    log.warning("model replied without a tool call (attempt %d/%d): %r", attempt + 1, EXTRA_ATTEMPTS + 1, text[:160])
                    if attempt < EXTRA_ATTEMPTS:
                        response = await original(*args, **relaxed)
                return response

            messages.create = create
            messages._nami_relaxed = True
        return client
