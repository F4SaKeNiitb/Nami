"""OpenAI as the model provider instead of Anthropic. Written, not run (test pause); the behaviours were exercised once by hand with a fake API
(no key was available, so nothing was sent to OpenAI)."""

import inspect
from pathlib import Path
from types import SimpleNamespace as N

import pytest
from pydantic import BaseModel

from app.core.config import Settings
from app.core.llm import AnthropicLLM, LLMError, OpenAILLM, RoutedLLM
from app.core.runtime import MissingKeyLLM, build_llm


def settings(**kw):
    # The suite is often run with the developer's real .env loaded into the
    # process. Make each case choose its provider only from the inputs below.
    kw.setdefault("openai_api_key", None)
    kw.setdefault("anthropic_api_key", None)
    kw.setdefault("llm_provider", "auto")
    return Settings(_env_file=None, **kw)


class Out(BaseModel):
    answer: str


class FakeCompletions:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    async def create(self, **kw):
        self.calls.append(kw)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return N(choices=[N(message=N(content=r))], usage=N(prompt_tokens=11, completion_tokens=7))


def llm(replies, **kw):
    c = FakeCompletions(replies)
    return OpenAILLM("k", "gpt-4.1-mini", client=N(chat=N(completions=c)), **kw), c


# ───────────── which provider, which models ─────────────

def test_an_openai_key_selects_openai_and_its_default_models():
    s = settings(openai_api_key="k")
    assert s.provider == "openai" and s.llm_api_key == "k"
    assert (s.model_main, s.model_fast, s.model_strong) == ("gpt-6-luna", "gpt-6-luna", "gpt-5")


def test_openai_wins_when_both_keys_are_present_because_it_was_asked_for():
    assert settings(openai_api_key="o", anthropic_api_key="a").provider == "openai"


def test_with_only_an_anthropic_key_nothing_changes_for_existing_setups():
    s = settings(anthropic_api_key="a")
    assert s.provider == "anthropic" and s.llm_api_key == "a" and s.model_main.startswith("claude-")


def test_no_key_at_all_keeps_the_old_defaults():
    s = settings()
    assert s.provider == "anthropic" and s.llm_api_key is None and s.model_main.startswith("claude-")


def test_the_provider_can_be_forced_either_way():
    assert settings(openai_api_key="o", anthropic_api_key="a", llm_provider="anthropic").provider == "anthropic"
    assert settings(openai_api_key="o", anthropic_api_key="a", llm_provider="anthropic").llm_api_key == "a"
    assert settings(anthropic_api_key="a", llm_provider="openai").provider == "openai"


def test_an_explicit_model_name_is_never_overridden():
    s = settings(openai_api_key="k", model_main="gpt-4.1", model_strong="gpt-4.1-mini")
    assert (s.model_main, s.model_fast, s.model_strong) == ("gpt-4.1", "gpt-6-luna", "gpt-4.1-mini")


def test_the_env_example_documents_the_openai_key():
    text = Path(".env.example").read_text()
    assert "NAMI_OPENAI_API_KEY" in text and "NAMI_LLM_PROVIDER" in text


# ───────────── the OpenAI client ─────────────

async def test_a_reply_is_parsed_into_the_schema_in_json_mode_and_usage_is_counted():
    client, calls = llm(['{"answer": "ok"}'])
    assert (await client.json(system="s", user="u", schema=Out)).answer == "ok"
    k = calls.calls[0]
    assert k["response_format"] == {"type": "json_object"} and k["max_completion_tokens"] == 1500
    assert "reasoning_effort" not in k and "JSON" in k["messages"][0]["content"]       # JSON mode needs the word in the prompt
    assert client.usage == {"input_tokens": 11, "output_tokens": 7, "calls": 1}


async def test_a_screenshot_goes_as_an_image_url_data_uri():
    client, calls = llm(['{"answer": "ok"}'])
    await client.json(system="s", user="u", schema=Out, images=[b"\x89PNG"])
    part = calls.calls[0]["messages"][1]["content"][1]
    assert part["type"] == "image_url" and part["image_url"]["url"].startswith("data:image/png;base64,")


async def test_bad_json_gets_one_repair_attempt_then_a_clean_error():
    client, calls = llm(["not json", '```json\n{"answer": "fixed"}\n```'])
    assert (await client.json(system="s", user="u", schema=Out)).answer == "fixed"
    assert len(calls.calls) == 2 and "invalid" in calls.calls[1]["messages"][-1]["content"]
    client, _ = llm(["nope", "still nope"])
    with pytest.raises(LLMError, match="did not return valid Out"):
        await client.json(system="s", user="u", schema=Out)


async def test_a_thinking_model_gets_reasoning_effort_and_room_for_its_reasoning():
    client, calls = llm(['{"answer": "x"}'], reasoning_effort="low")
    await client.json(system="s", user="u", schema=Out, model="gpt-5-mini")
    k = calls.calls[0]
    assert k["reasoning_effort"] == "low" and k["max_completion_tokens"] > 1500 and k["model"] == "gpt-5-mini"
    client, calls = llm(['{"answer": "x"}'])
    await client.json(system="s", user="u", schema=Out, model="o3-mini")
    assert "reasoning_effort" in calls.calls[0]


async def test_a_failed_call_is_an_llm_error_and_never_carries_a_key():
    client, _ = llm([RuntimeError("Incorrect API key provided: sk-proj-abcdefghijklmnopqrstuvwxyz0123456789")])
    with pytest.raises(LLMError) as e:
        await client.json(system="s", user="u", schema=Out)
    assert "sk-proj-abcdef" not in str(e.value) and "the model call failed" in str(e.value)


async def test_the_per_call_model_overrides_the_default():
    client, calls = llm(['{"answer": "x"}'])
    await client.json(system="s", user="u", schema=Out, model="gpt-4.1")
    assert calls.calls[0]["model"] == "gpt-4.1"


# ───────────── the wiring ─────────────

def test_the_runtime_builds_the_right_client_for_the_provider():
    assert isinstance(build_llm(settings()), MissingKeyLLM)
    assert isinstance(build_llm(settings(openai_api_key="k")), OpenAILLM)
    assert isinstance(build_llm(settings(anthropic_api_key="a")), AnthropicLLM)
    assert isinstance(build_llm(settings(openai_api_key="k", anthropic_api_key="a", llm_provider="anthropic")), AnthropicLLM)


def test_the_free_route_still_wraps_whichever_provider_is_primary():
    s = settings(openai_api_key="k", openrouter_api_key="r", free_model="some/free")
    out = build_llm(s)
    assert isinstance(out, RoutedLLM) and isinstance(out.primary, OpenAILLM)


async def test_the_missing_key_message_names_both_options_and_keeps_the_phrase_the_ui_test_looks_for():
    with pytest.raises(LLMError) as e:
        await MissingKeyLLM().json()
    assert "No model key" in str(e.value) and "NAMI_OPENAI_API_KEY" in str(e.value)


def test_the_browser_agent_uses_the_client_of_the_provider(monkeypatch):
    from app.browser import executor as ex
    made = {}
    monkeypatch.setattr(ex, "ChatOpenAI", lambda **kw: made.setdefault("openai", kw) or object())
    monkeypatch.setattr(ex, "AutoToolChoiceAnthropic", lambda **kw: made.setdefault("anthropic", kw) or object())
    ex.Executor(model="gpt-4.1-mini", provider="openai", api_key="sk-x")._llm()
    ex.Executor(anthropic_api_key="a-key", model="claude-x")._llm()                  # the old call still means Anthropic
    assert made["openai"]["model"] == "gpt-4.1-mini" and made["openai"]["api_key"] == "sk-x"
    assert "gpt-6" in made["openai"]["reasoning_models"] and "gpt-5" in made["openai"]["reasoning_models"]
    assert made["anthropic"] == {"model": "claude-x", "api_key": "a-key"}


def test_the_research_harness_passes_the_right_provider_and_key_name():
    from app.harness import pi_runner as pr
    assert pr.PiHarness.KEY_ENV == {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
    h = pr.PiHarness(None, api_key="k", model="gpt-4.1-mini", provider="openai")
    assert h.provider == "openai"
    assert pr.PiHarness(None, api_key="k", model="m", provider="something-else").provider == "anthropic"      # unknown: safe default
    src = inspect.getsource(pr.PiHarness.run)
    assert 'self.KEY_ENV[self.provider]' in src and '"--provider", self.provider' in src


def test_the_azure_bootstrap_selects_openai_and_the_secret_script_asks_for_a_model_key():
    assert "NAMI_LLM_PROVIDER=openai" in Path("infra/azure-vm/bootstrap.sh").read_text()
    fetch = Path("infra/azure-vm/fetch-secrets.sh").read_text()
    assert "openai-api-key" in fetch and "NAMI_OPENAI_API_KEY" in fetch and "no model key" in fetch
