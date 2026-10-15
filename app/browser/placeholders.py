"""The agent may only type secret placeholders that exist in this run.

A credential is typed as `<secret>name</secret>`; browser-use swaps in the real value. Found live (Swiggy, with gpt-4.1-mini): at the one-time-code
screen the agent typed `<secret>otp_code</secret>`, a placeholder that does not exist because the user had not been asked for a code yet. It went on
for three minutes, hit the step limit, and the failure was read as "the saved login was rejected", so the user was never asked for the code.

Here a placeholder that is not in the run's own list never reaches the page. If its name looks like a code, that is the agent saying it is at a
verification-code box, so the run pauses and asks the user (the same as `request_help(kind='code')`). Any other unknown name is refused with the list
of names that do exist. The names come from the broker, which only holds what the user's access plan approved, so a page cannot widen them.
"""

import re

_PLACEHOLDER = re.compile(r"<secret>\s*([^<>\s]+)\s*</secret>")
_LOOKS_LIKE_A_CODE = re.compile(r"otp|code|verif|passcode|pin|token|2fa|mfa|sms", re.I)

# A verification-code input, judged from the element's own attributes (never from page text, which can say anything).
_CODE_FIELD = re.compile(r"otp|one[- ]?time|verification|verify|passcode|security[- ]?code|(?<![a-z0-9])code(?![a-z0-9])|2fa|mfa", re.I)
_NOT_A_CODE = re.compile(r"promo|coupon|referral|invite|gift|voucher|discount|zip|postal|pin ?code", re.I)
_ONE_TIME_NAME = re.compile(r"^(?:otp|sms|verification|authenticator|mfa|2fa)[_-]?code$|_2fa_code$", re.I)


def element_looks_like_code_field(attrs: dict) -> bool:
    if str(attrs.get("autocomplete", "")).lower() == "one-time-code":
        return True
    blob = " ".join(str(attrs.get(k, "")) for k in ("name", "id", "placeholder", "aria-label", "title", "data-testid"))
    return bool(_CODE_FIELD.search(blob)) and not _NOT_A_CODE.search(blob)


# the action and the parameter that carries what is typed
TYPING_ACTIONS = {"input": "text", "send_keys": "keys"}


def placeholder_names(text: str) -> list[str]:
    return _PLACEHOLDER.findall(text or "")


def unknown_placeholders(text: str, known: set[str]) -> list[str]:
    return [n for n in placeholder_names(text) if n not in known]


CODE_PLACEHOLDER = "otp_code"          # the name the orchestrator gives a code the user has just typed (it exists only for that run)


def code_instruction(known: set[str]) -> str | None:
    """What to tell the agent when it tries to type a code itself, if the user has ALREADY given one for this run: use the placeholder, never
    digits. None when no code was given: then the right move is to ask the user. (Found live: with the code already supplied, the agent typed
    digits, the guard paused for the user, and the user was asked for a second code they had just sent.)"""
    if CODE_PLACEHOLDER not in known:
        return None
    return (f"The verification code is available only as the placeholder <secret>{CODE_PLACEHOLDER}</secret>. Type exactly that into the code box; "
            "never type digits or a name you made up.")


def looks_like_a_code(names: list[str]) -> bool:
    return any(_LOOKS_LIKE_A_CODE.search(n) for n in names)


def install(tools, *, known: set[str], on_unknown, on_code_field=None, known_values: frozenset[str] = frozenset(),
            one_time_values: frozenset[str] = frozenset(), on_one_time_reuse=None) -> list[str]:
    """Wrap the typing actions. `on_unknown(names)` is awaited when a text names a placeholder that does not exist, and whatever it returns
    (an ActionResult) is handed back to the agent instead of typing anything. `on_code_field(attrs)` is awaited when the agent is about to type
    anything that is not one of the run's own values into a verification-code box: a code the agent made up is never typed (found live: it typed
    one, saw "wrong OTP", and tried to resend, without the user ever being asked).

    IMPORTANT: browser-use swaps `<secret>name</secret>` for the real value BEFORE it calls the action, so the guard sees the real digits, not the
    placeholder. `known_values` are the run's real values (the user's code, the phone number...): typing exactly one of those is the placeholder
    working, and is always allowed. (Found live: without this the guard blocked the user's own code every time, for three runs in a row.)
    Returns the actions that were wrapped."""
    wrapped = []
    used_one_time_values: set[str] = set()
    for name, field in TYPING_ACTIONS.items():
        action = tools.registry.registry.actions.get(name)
        if action is None:
            continue
        original = action.function

        async def guarded(params, _orig=original, _field=field, **ctx):
            bad = unknown_placeholders(str(getattr(params, _field, "") or ""), known)
            if bad:
                return await on_unknown(bad)
            value = str(getattr(params, _field, "") or "")
            if value in one_time_values and value in used_one_time_values:
                if on_one_time_reuse is not None:
                    return await on_one_time_reuse()
                return ActionResult(is_done=True, success=False, extracted_content="This one-time code was already used. Ask the user for a fresh code.")
            if on_code_field is not None and _field == "text" and not placeholder_names(str(params.text)) and str(params.text) not in known_values:
                try:
                    node = await ctx["browser_session"].get_element_by_index(params.index)
                    attrs = dict(getattr(node, "attributes", None) or {})
                except Exception:
                    attrs = {}                              # cannot tell which box it is: do not block ordinary typing
                if element_looks_like_code_field(attrs):
                    return await on_code_field(attrs)
            result = await _orig(params=params, **ctx)
            if value in one_time_values and not getattr(result, "error", None):
                used_one_time_values.add(value)
            return result

        action.function = guarded
        wrapped.append(name)
    return wrapped
