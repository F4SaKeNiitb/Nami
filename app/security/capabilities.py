"""Layer 4: what one tool call may do, decided from the current subgoal. Pure code, no model, no I/O.

The executor's step hook can call `check_call` before each browser action. The access plan already limits WHERE the browser
may go; this limits WHAT it may do there:
  read-only subgoal  : navigate, click, scroll, read; no typing, no file transfer
  sign-in subgoal    : typing is allowed, but only placeholder text (never a literal the model made up or copied from a page)
  irreversible step  : same as above, and only once the user approved that step
Anything not recognised is denied.
"""

from dataclasses import dataclass

from app.agent.schemas import Subgoal
from app.security.gateway import url_in_plan

_READ = {"navigate", "go_to_url", "go_back", "scroll", "scroll_down", "scroll_up", "wait", "screenshot", "extract", "extract_content",
         "switch", "switch_tab", "close_tab", "click", "click_element", "click_element_by_index", "done", "request_help",
         "get_dropdown_options", "dropdown_options", "find_text", "search_page", "read_long_content", "ask_user",
         "confirm_action", "close", "wait_for"}
_TYPE = {"input", "input_text", "send_keys", "select_dropdown", "select_dropdown_option"}
_NAVIGATES = {"navigate", "go_to_url"}
_FILES = ("upload", "download", "save_as_pdf", "write_file", "read_file", "replace_file")
_ALWAYS_DENIED = {"evaluate", "find_elements", "search"}


_MODIFIERS = {"control", "ctrl", "shift", "alt", "meta", "command", "cmd", "controlormeta"}
_KEY_NAMES = {"enter", "return", "tab", "escape", "esc", "backspace", "delete", "del", "home", "end", "pageup", "pagedown", "insert",
              "arrowup", "arrowdown", "arrowleft", "arrowright", "up", "down", "left", "right", "space"}


def keys_type_text(keys: str) -> bool:
    """`send_keys` either presses keys (Enter, Tab, F5, Control+A) or types a string. Only a string is typing: pressing Enter to
    submit a sign-in form must not be refused as "typed a literal" (found in the first live run)."""
    k = (keys or "").strip()
    if not k:
        return False
    parts = [p.strip().lower() for p in k.split("+")] if len(k) > 1 else [k.lower()]
    for i, p in enumerate(parts):
        last = i == len(parts) - 1
        if p in _KEY_NAMES or (p.startswith("f") and p[1:].isdigit() and 1 <= int(p[1:]) <= 12):
            continue
        if p in _MODIFIERS and not last:
            continue
        if last and len(parts) > 1 and len(p) == 1:      # a shortcut such as Control+a
            continue
        return True
    return False


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str = ""


def _name(action: str) -> str:
    return (action or "").strip().lower()


def check_call(subgoal: Subgoal, action: str, *, url: str = "", types_text: bool | None = None, text_is_placeholder: bool = False,
               allowed_domains: list[str] | None = None, approved: bool = False) -> Decision:
    """`action` is the tool name; `url` the target of a navigation; `types_text` whether text goes into the page (derived from the
    name when None); `text_is_placeholder` is true only when the typed text is a `<secret>name</secret>` placeholder."""
    a = _name(action)
    if not a:
        return Decision(False, "no action name")
    if a in _ALWAYS_DENIED:
        return Decision(False, f"{a} is never allowed")
    if any(w in a for w in _FILES):
        return Decision(False, "file transfer is not allowed in any step")
    typing = a in _TYPE if types_text is None else types_text
    if a not in _READ and a not in _TYPE:
        return Decision(False, f"unknown action {a!r}")
    if a in _NAVIGATES and allowed_domains is not None and not url_in_plan(url, allowed_domains):
        return Decision(False, "navigation outside the allowed sites")
    if typing:
        if subgoal.irreversible and not approved:
            return Decision(False, "this step needs the user's approval first")
        if subgoal.irreversible or subgoal.is_auth:
            return Decision(True) if text_is_placeholder else Decision(False, "only saved placeholders may be typed in this step")
        return Decision(True)          # a reversible step may fill a form; sending is a separate approved step
    if subgoal.irreversible and not approved and a.startswith("click"):
        return Decision(False, "this step needs the user's approval first")
    return Decision(True)
