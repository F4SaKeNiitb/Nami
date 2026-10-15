"""The browser-use Claude client: forced tool_choice is relaxed, flattened answers are repaired, nothing else is touched."""

import asyncio
from types import SimpleNamespace

import pytest

import app.browser.llm as L
from app.browser import executor as ex

SCHEMA = {"properties": {"action": {"items": {"anyOf": [
    {"properties": {"navigate": {"properties": {"url": {"type": "string"}, "new_tab": {"type": "boolean"}}, "required": ["url", "new_tab"]}}},
    {"properties": {"extract": {"properties": {"query": {"type": "string"}, "extract_links": {"type": "boolean"},
                                                "start_from_char": {"type": "integer"}}, "required": ["query"]}}},
    {"properties": {"search_page": {"properties": {"pattern": {"type": "string"},
                                                   "css_scope": {"anyOf": [{"type": "string"}, {"type": "null"}]}}, "required": ["pattern"]}}},
    {"properties": {"done": {"properties": {"text": {"type": "string"}, "success": {"type": "boolean"}}, "required": ["text", "success"]}}},
]}}}}


def test_a_forced_tool_choice_becomes_auto_with_a_format_reminder():
    kw = {"tool_choice": {"type": "tool", "name": "AgentOutput"}, "system": "be careful", "tools": [{"name": "AgentOutput"}]}
    out = L.relax_tool_choice(kw)
    assert out["tool_choice"] == {"type": "auto"}
    assert out["system"].startswith("be careful") and "OUTPUT FORMAT" in out["system"]
    assert kw["tool_choice"]["type"] == "tool"                       # the caller's dict is not mutated


def test_calls_that_did_not_force_a_tool_are_left_alone():
    kw = {"tool_choice": {"type": "auto"}, "system": "x"}
    assert L.relax_tool_choice(kw) is kw
    assert L.relax_tool_choice({"system": "x"}) == {"system": "x"}


def test_a_list_system_prompt_gets_the_reminder_as_a_block():
    out = L.relax_tool_choice({"tool_choice": {"type": "any"}, "system": [{"type": "text", "text": "a"}]})
    assert out["system"][-1]["type"] == "text" and "OUTPUT FORMAT" in out["system"][-1]["text"]


def test_a_bare_action_is_wrapped_into_the_action_list():
    fixed = L.repair_tool_input(SCHEMA, {"query": "the heading", "extract_links": "true", "start_from_char": "0"})
    assert fixed["action"] == [{"extract": {"query": "the heading", "extract_links": "true", "start_from_char": "0"}}]
    assert {"thinking", "evaluation_previous_goal", "memory", "next_goal"} <= set(fixed)


def test_the_models_own_thinking_fields_are_kept_when_wrapping():
    fixed = L.repair_tool_input(SCHEMA, {"thinking": "t", "next_goal": "g", "url": "https://a.com", "new_tab": False})
    assert fixed["thinking"] == "t" and fixed["next_goal"] == "g"
    assert fixed["action"] == [{"navigate": {"url": "https://a.com", "new_tab": False}}]


def test_an_ambiguous_or_unknown_shape_is_not_guessed():
    assert L.repair_tool_input(SCHEMA, {"nonsense": 1}) == {"nonsense": 1}
    two = {"properties": {"action": {"items": {"anyOf": [
        {"properties": {"a": {"properties": {"x": {}}, "required": []}}}, {"properties": {"b": {"properties": {"x": {}}, "required": []}}}]}}}}
    assert L.repair_tool_input(two, {"x": 1}) == {"x": 1}


def test_a_well_formed_answer_is_not_changed_except_null_strings():
    good = {"thinking": "t", "action": [{"navigate": {"url": "https://a.com", "new_tab": False}}]}
    assert L.repair_tool_input(SCHEMA, good) == good
    out = L.repair_tool_input(SCHEMA, {"action": [{"search_page": {"pattern": "x", "css_scope": "null"}}]})
    assert out["action"][0]["search_page"]["css_scope"] is None


def test_the_string_null_stays_a_string_where_null_is_not_allowed():
    out = L.repair_tool_input(SCHEMA, {"action": [{"search_page": {"pattern": "null", "css_scope": "main"}}]})
    assert out["action"][0]["search_page"] == {"pattern": "null", "css_scope": "main"}   # pattern is not nullable


def test_non_dict_input_passes_through():
    assert L.repair_tool_input(SCHEMA, "text") == "text"
    assert L.repair_tool_input({}, {"query": "q"}) == {"query": "q"}


class _Messages:
    def __init__(self, content):
        self.seen, self.content = [], content

    async def create(self, **kw):
        self.seen.append(kw)
        return SimpleNamespace(content=self.content)


def _client(content):
    client = SimpleNamespace(messages=_Messages(content))
    obj = object.__new__(L.AutoToolChoiceAnthropic)
    L.ChatAnthropic.get_client = lambda self: client          # the underlying SDK client is faked
    return obj, client


def test_the_wrapper_relaxes_the_request_and_repairs_the_reply(monkeypatch):
    original = L.ChatAnthropic.get_client
    block = SimpleNamespace(type="tool_use", input={"query": "q", "extract_links": "true", "start_from_char": "0"})
    try:
        obj, client = _client([block])
        c = obj.get_client()
        resp = asyncio.run(c.messages.create(tools=[{"name": "AgentOutput", "input_schema": SCHEMA}],
                                             tool_choice={"type": "tool", "name": "AgentOutput"}, system="s", messages=[]))
        assert client.messages.seen[0]["tool_choice"] == {"type": "auto"}
        assert resp.content[0].input["action"][0].keys() == {"extract"}
        assert obj.get_client() is c and c.messages._nami_relaxed         # wrapped once, not on every call
    finally:
        L.ChatAnthropic.get_client = original


def test_the_wrapper_does_not_touch_replies_to_calls_it_did_not_relax():
    original = L.ChatAnthropic.get_client
    block = SimpleNamespace(type="tool_use", input={"query": "q"})
    try:
        obj, client = _client([block])
        resp = asyncio.run(obj.get_client().messages.create(tools=[{"name": "x", "input_schema": SCHEMA}], messages=[]))
        assert resp.content[0].input == {"query": "q"}
    finally:
        L.ChatAnthropic.get_client = original


# ── what the agent is NOT allowed to do ──

@pytest.mark.parametrize("action", ["evaluate", "upload_file", "read_file", "write_file", "replace_file", "save_as_pdf", "search"])
def test_dangerous_or_off_plan_actions_are_not_offered_to_the_model(action):
    from browser_use import Tools
    offered = set(Tools(exclude_actions=ex.EXCLUDED_ACTIONS).registry.registry.actions)
    assert action not in offered
    assert {"navigate", "click", "input", "done"} <= offered          # the useful ones are still there


def test_the_executor_turns_the_browser_use_judge_off():
    import inspect
    assert "use_judge=False" in inspect.getsource(ex.Executor.run)


# ── model answers in prose instead of calling the tool ──

class _Seq:
    """messages.create that returns a scripted list of replies, one per call."""
    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0

    async def create(self, **kw):
        self.calls += 1
        return SimpleNamespace(content=self.replies.pop(0) if len(self.replies) > 1 else self.replies[0])


def _wrapped(replies):
    seq = _Seq(replies)
    original = L.ChatAnthropic.get_client
    L.ChatAnthropic.get_client = lambda self: SimpleNamespace(messages=seq)
    obj = object.__new__(L.AutoToolChoiceAnthropic)
    return obj, seq, original


def _call(obj):
    return asyncio.run(obj.get_client().messages.create(
        tools=[{"name": "AgentOutput", "input_schema": SCHEMA}], tool_choice={"type": "tool", "name": "AgentOutput"}, system="s", messages=[]))


TEXT = SimpleNamespace(type="text", text="Here is what I found, in prose.")
TOOL = SimpleNamespace(type="tool_use", input={"thinking": "t", "action": [{"navigate": {"url": "https://a.com", "new_tab": False}}]})


def test_a_prose_reply_is_retried_until_the_tool_is_called():
    obj, seq, original = _wrapped([[TEXT], [TEXT], [TOOL]])
    try:
        resp = _call(obj)
    finally:
        L.ChatAnthropic.get_client = original
    assert seq.calls == 3 and resp.content[0].type == "tool_use"


def test_retrying_is_bounded_and_the_last_reply_is_returned_unchanged():
    obj, seq, original = _wrapped([[TEXT]])
    try:
        resp = _call(obj)
    finally:
        L.ChatAnthropic.get_client = original
    assert seq.calls == L.EXTRA_ATTEMPTS + 1 and resp.content == [TEXT]       # no infinite loop, no invented tool call


def test_a_json_object_in_the_prose_is_salvaged_as_the_tool_input():
    body = 'Sure. {"thinking": "t", "next_goal": "g", "action": [{"navigate": {"url": "https://a.com", "new_tab": false}}]}'
    obj, seq, original = _wrapped([[SimpleNamespace(type="text", text=body)]])
    try:
        resp = _call(obj)
    finally:
        L.ChatAnthropic.get_client = original
    assert seq.calls == 1 and resp.content[0].type == "tool_use" and resp.content[0].name == "AgentOutput"
    assert resp.content[0].input["action"][0]["navigate"]["url"] == "https://a.com"


@pytest.mark.parametrize("text", ["no json here", "{not json}", '{"a": 1}', "[1, 2]"])
def test_prose_without_a_usable_action_object_is_not_salvaged(text):
    assert L._salvage(text) is None


def test_a_good_first_reply_costs_exactly_one_call():
    obj, seq, original = _wrapped([[TOOL]])
    try:
        _call(obj)
    finally:
        L.ChatAnthropic.get_client = original
    assert seq.calls == 1


# ── nothing is sent to third parties by the browser library ──

def test_browser_use_telemetry_and_version_check_are_off_in_the_real_library():
    import subprocess
    import sys
    out = subprocess.run([sys.executable, "-c",
                          "import app.browser.llm, os\nfrom browser_use.config import CONFIG\n"
                          "print(CONFIG.ANONYMIZED_TELEMETRY, CONFIG.BROWSER_USE_VERSION_CHECK)"],
                         capture_output=True, text=True, env={**__import__('os').environ, "PYTHONPATH": "."}, timeout=60)
    assert out.stdout.strip().splitlines()[-1] == "False False", out.stdout + out.stderr


# ── the whole answer written inside one string field (seen live on a Gmail task) ──

NESTED = ('Let me analyze the page. The compose window is open {with a To field}.\n```json\n'
          '{"thinking": "click the To field", "evaluation_previous_goal": "ok", "memory": "m", "next_goal": "type the address",\n'
          ' "action": [{"input": {"index": 12, "text": "a@b.com", "clear": true}}]}\n```')


def test_a_complete_answer_written_inside_the_thinking_field_is_recovered():
    fixed = L.repair_tool_input(SCHEMA, {"thinking": NESTED})
    assert fixed["action"] == [{"input": {"index": 12, "text": "a@b.com", "clear": True}}]
    assert fixed["next_goal"] == "type the address"


def test_reasoning_that_contains_other_braces_does_not_hide_the_real_object():
    text = 'Consider {a} and {"x": 1} first.\n{"action": [{"done": {"text": "ok", "success": true}}]}'
    assert L._salvage(text) == {"action": [{"done": {"text": "ok", "success": True}}]}


def test_a_nested_object_without_an_action_list_is_ignored():
    assert L.repair_tool_input(SCHEMA, {"thinking": 'I will {"x": 1} do nothing'}) == {"thinking": 'I will {"x": 1} do nothing'}
    assert L._salvage('{"action": "not a list"}') is None


def test_salvage_is_bounded_on_a_pathological_reply():
    assert L._salvage("{" * 100000) is None            # must return, not hang


def test_nested_actions_get_null_strings_normalised_too():
    text = '{"action": [{"search_page": {"pattern": "x", "css_scope": "null"}}]}'
    assert L.repair_tool_input(SCHEMA, {"thinking": text})["action"][0]["search_page"]["css_scope"] is None


def test_a_prose_reply_with_a_fenced_answer_is_salvaged_by_the_wrapper():
    obj, seq, original = _wrapped([[SimpleNamespace(type="text", text=NESTED)]])
    try:
        resp = _call(obj)
    finally:
        L.ChatAnthropic.get_client = original
    assert seq.calls == 1 and resp.content[0].input["action"][0].keys() == {"input"}


# ── answers whose final closing bracket is missing (seen live: the nested JSON ended `...[]}}]` with no last `}`) ──

TRUNC = 'Let me analyze.\n{"thinking": "t", "action": [{"done": {"text": "ok", "success": true, "files_to_display": []}}]\n'


def test_a_nested_answer_missing_its_last_brace_is_recovered():
    out = L.repair_tool_input(SCHEMA, {"thinking": TRUNC})
    assert out["action"] == [{"done": {"text": "ok", "success": True, "files_to_display": []}}]


def test_up_to_three_missing_closers_are_completed_but_no_more():
    assert L._salvage('{"action": [{"x": {"y": 1}') == {"action": [{"x": {"y": 1}}]}       # one closer missing: "]"... then "}"
    assert L._missing_closers('{"a": [{"action": [{"b": {"c": 1') == "}}]}]}"              # six missing: far past the limit
    assert L._salvage('{"action": [{"x": {"y": {"z": {"w": 1') is None                    # more than three: not guessed


def test_text_cut_inside_a_string_is_never_completed():
    assert L._salvage('{"thinking": "t", "action": [{"done": {"text": "cut here') is None


def test_brackets_inside_strings_and_escaped_quotes_do_not_confuse_the_completer():
    text = r'{"thinking": "a } ] \" { [ b", "action": [{"done": {"text": "x \"quoted\" }", "success": true}}]'
    got = L._salvage(text)
    assert got is not None and got["action"][0]["done"]["text"] == 'x "quoted" }'


def test_a_trailing_code_fence_after_the_truncated_json_is_ignored():
    got = L._salvage('```json\n{"thinking": "t", "action": [{"done": {"text": "ok", "success": true}}]\n```')
    assert got is not None and got["action"][0]["done"]["text"] == "ok"


@pytest.mark.parametrize("fragment", ['{"action": [}', '{"action": ]]', '{"nope": 1', 'plain text', '{"action": "not a list"'])
def test_unbalanced_or_irrelevant_fragments_return_nothing(fragment):
    assert L._salvage(fragment) is None


def test_completion_work_is_bounded_on_a_pathological_reply():
    assert L._salvage('{"action": [' * 5000) is None
    assert L._salvage("[" * 200000) is None                       # deep nesting must not raise RecursionError
    assert L._salvage("x" * 5_000_000 + '{"action": []}') is None   # only the first MAX_SCAN_CHARS are examined
