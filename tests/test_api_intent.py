"""A request to generate with a model, or to use an API key, on a site with an API definition goes to the key, not the website."""

import pytest

from app.agent.access_planner import build_access_plan, wants_api_route
from app.agent.schemas import Intent
from app.rules.engine import EffectiveRules
from app.vault.service import CredentialView
from datetime import datetime


def _rules():
    return EffectiveRules(org_id="o", user_id="u", site=None, version="v", policies={})


def _key(site="openrouter.ai"):
    return CredentialView("k1", "api_key", "OpenRouter", [site], {"last4": "abcd"}, datetime(2026, 1, 1), None, False)


POEM = "can you generate me a poem on Claude using openrouter api key and Nemotron model"


def test_generate_on_a_site_with_an_api_block_wants_the_api_route_even_if_needs_login_is_false():
    assert wants_api_route(Intent(goal="poem", sites=["openrouter.ai"], generate=True), POEM)


def test_saying_api_key_is_enough():
    for text in ("check my credits with my API key", "use my api-key on openrouter", "my apikey for openrouter"):
        assert wants_api_route(Intent(goal="credits", sites=["openrouter.ai"]), text), text


@pytest.mark.parametrize("intent,text", [
    (Intent(goal="x", sites=["swiggy.com"], generate=True), "generate me a poem"),               # no API block for the site
    (Intent(goal="x", sites=["swiggy.com"]), "use my api key"),
    (Intent(goal="x", sites=["openrouter.ai"]), "open openrouter and look at the pricing page"),   # an ordinary browse stays a browse
    (Intent(goal="x", sites=[]), "use my api key"),
])
def test_everything_else_keeps_its_route(intent, text):
    assert not wants_api_route(intent, text)


def test_the_two_steps_together_pick_the_api_with_a_saved_key_and_ask_for_one_without():
    base = Intent(goal="poem", sites=["openrouter.ai"], needs_login=False, generate=True)
    assert build_access_plan(base, [_key()], _rules(), session_sites=set()).login == "none"          # the old behaviour: the browser
    fixed = base.model_copy(update={"needs_login": True})
    p = build_access_plan(fixed, [_key()], _rules(), session_sites=set())
    assert p.login == "api" and p.credentials[0].kind == "api_key"
    asked = build_access_plan(fixed, [], _rules(), session_sites=set())
    assert asked.login == "need_credentials" and asked.ask.kinds[0] == "api_key"                      # no key saved: ask for the key


# ───────────── a service the user names outright is never dropped by the model (written, not run) ─────────────
# Found live: 'generate poem on "Claude" using my openrouter API key' came back with no site, and the task stopped with "I need to know which
# website to use". The user named OpenRouter in plain words.

import pytest as _pytest                                                              # noqa: E402

from app.sites.loader import sites_named                                              # noqa: E402


@_pytest.mark.parametrize("text,expected", [
    ('generate poem on "Claude" using my openrouter API key', ["openrouter.ai"]),
    ("openrouter api", ["openrouter.ai"]),
    ("OpenRouter's credits", ["openrouter.ai"]),
    ("login to swiggy and check my last order", ["swiggy.com"]),
    ("deploy to Vercel and tell me the swiggy order", ["vercel.com", "swiggy.com"]),        # in the order the user said them
    ("check my anthropic key", ["anthropic.com"]),
])
def test_services_named_in_the_request_are_found_from_the_users_words_alone(text, expected):
    assert sites_named(text) == expected


@_pytest.mark.parametrize("text", ["hi", "what's your name", "google it", "my openrouterfoo thing", "swiggyish", "", "what can you do?"])
def test_nothing_is_invented_for_small_talk_look_alikes_or_the_ambiguous_word_google(text):
    assert sites_named(text) == []


async def test_a_site_the_model_left_out_is_added_and_one_it_found_is_not_duplicated():
    from app.agent.intent import parse_intent
    from app.agent.schemas import Intent

    class Model:
        def __init__(self, sites):
            self.sites = sites

        async def json(self, **kw):
            return Intent(goal="g", sites=self.sites)
    missed = await parse_intent(Model([]), "generate a poem using my openrouter API key", model="m")
    assert missed.sites == ["openrouter.ai"]
    found = await parse_intent(Model(["openrouter.ai"]), "check my openrouter credits", model="m")
    assert found.sites == ["openrouter.ai"]
    other = await parse_intent(Model(["swiggy.com"]), "compare swiggy with openrouter", model="m")
    assert other.sites == ["swiggy.com", "openrouter.ai"]


async def test_small_talk_still_gets_no_site():
    from app.agent.intent import parse_intent
    from app.agent.schemas import Intent

    class Model:
        async def json(self, **kw):
            return Intent(goal="greet", sites=[])
    assert (await parse_intent(Model(), "hi", model="m")).sites == []


def test_the_intent_prompt_says_not_to_ask_about_optional_details():
    """Smarter models over-ask ("which Claude model?", "any preferred style?"). Defaults are picked instead."""
    from app.agent.intent import SYSTEM
    assert "Never ask about optional preferences" in SYSTEM and "pick the default and go" in SYSTEM


def test_the_request_reader_is_a_smarter_model_than_gpt_4_1():
    from app.core.config import Settings
    s = Settings(_env_file=None, openai_api_key="k")
    assert s.model_fast == "gpt-6-luna" and s.model_fast != "gpt-4.1"


# ───────────── fixes from the live end-to-end queries (written, not run) ─────────────

def _rules():
    from app.rules.engine import EffectiveRules
    return EffectiveRules(org_id="o", user_id="u", site="openrouter.ai", version="v1")


def test_generating_with_a_site_asks_for_its_api_key_only():
    """Found live: the card for 'generate a poem with my openrouter key' asked for a password and 2FA key and explained Google sign-in."""
    from app.agent.access_planner import credential_ask
    from app.agent.schemas import Intent
    from app.sites.loader import hints_for
    ask = credential_ask("openrouter.ai", hints_for("openrouter.ai"), _rules(), Intent(goal="g", sites=["openrouter.ai"], generate=True))
    assert ask.kinds == ["api_key"] and ask.allow_takeover is False and ask.google_signin is False
    assert "API key" in ask.reason and "Google" not in ask.reason and "ask before spending credits" in ask.reason


def test_a_non_generate_request_on_the_same_site_still_offers_a_login_too():
    from app.agent.access_planner import credential_ask
    from app.agent.schemas import Intent
    from app.sites.loader import hints_for
    ask = credential_ask("openrouter.ai", hints_for("openrouter.ai"), _rules(), Intent(goal="g", sites=["openrouter.ai"], needs_login=True))
    assert "api_key" in ask.kinds and "password" in ask.kinds


def test_a_request_with_no_site_gets_a_helpful_reply_not_a_flat_refusal():
    from app.agent.access_planner import NO_SITE_MESSAGE, build_access_plan
    from app.agent.schemas import Intent
    plan = build_access_plan(Intent(goal="hi", sites=[]), [], _rules(), session_sites=set())
    assert plan.blocked == NO_SITE_MESSAGE and "for example" in NO_SITE_MESSAGE and "OpenRouter" in NO_SITE_MESSAGE
    assert NO_SITE_MESSAGE != "I need to know which website to use."


def test_the_planner_is_told_success_criteria_must_be_things_the_checker_can_see():
    """Found live: 'a visible Sign in button' could never be met, because a button's label is not page text; four correct sign-in pages were rejected."""
    from app.agent.planner import SYSTEM
    assert "text that WOULD APPEAR" in SYSTEM and "never make those part of the criterion" in SYSTEM


def test_the_checker_is_also_given_the_labels_of_the_buttons_and_links_on_the_page():
    import inspect
    from app.browser import executor
    from app.browser.login_probe import SCAN_JS
    assert "[Buttons and links on the page: " in inspect.getsource(executor.observe)
    assert "input[type=submit]" in SCAN_JS and "e.value" in SCAN_JS                 # <input type=submit value="Sign in"> counts


async def test_a_failure_to_read_the_controls_never_breaks_the_page_read():
    from types import SimpleNamespace as N
    from app.browser.executor import observe

    class Page:
        url = "https://github.com/login"

        async def evaluate(self, js):
            if "innerText" in js and "buttons" not in js:
                return "Sign in to GitHub\nUsername or email address\nPassword"
            raise RuntimeError("page navigated")

        async def screenshot(self, **kw):
            return b""

    class Ctx:
        pages = [Page()]

    class Browser:
        contexts = [Ctx()]

        async def close(self):
            pass
    import app.browser.cdp as cdp
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_connect(url):
        yield Browser()
    orig, cdp.connect = cdp.connect, fake_connect
    try:
        url, text, _ = await observe("ws://x", "", with_text=True, with_shot=False)
    finally:
        cdp.connect = orig
    assert "Username or email address" in text and "Buttons and links" not in text


# ───────────── fixes from the second round of live queries (written, not run) ─────────────

@_pytest.mark.parametrize("sites,allowed,expected", [
    (["news.ycombinator.com"], ["ycombinator.com"], ["https://news.ycombinator.com/"]),                    # the host the user meant, not the collapsed domain
    (["https://www.github.com/trending"], ["github.com"], ["https://www.github.com/"]),
    (["evil.com", "news.ycombinator.com"], ["ycombinator.com"], ["https://news.ycombinator.com/"]),        # never outside the plan's allowed domains
    (["ycombinator.com/jobs", "news.ycombinator.com"], ["ycombinator.com"], ["https://ycombinator.com/", "https://news.ycombinator.com/"]),
    ([], ["x.com"], []),
    (["a.x.com", "a.x.com", "b.x.com", "c.x.com", "d.x.com"], ["x.com"], ["https://a.x.com/", "https://b.x.com/", "https://c.x.com/"]),   # deduplicated, at most 3
])
def test_the_research_step_starts_at_the_host_the_user_named(sites, allowed, expected):
    """Found live: 'top stories on hacker news' reached the research step as ycombinator.com, which opened Y Combinator's front page, and the answer was a refusal."""
    from app.sites.loader import start_urls
    assert start_urls(sites, allowed) == expected


def test_the_start_urls_reach_the_research_harness_and_its_prompt():
    import inspect
    from app.agent.graph import Orchestrator
    from app.harness.pi_runner import PiHarness
    assert "start_urls=start_urls(intent.sites, access.allowed_domains)" in inspect.getsource(Orchestrator.n_explore)
    run_src = inspect.getsource(PiHarness.run)
    assert "start_urls: list[str] | None = None" in run_src and "Start here (the site the user means)" in run_src


def test_a_read_only_request_on_a_site_with_an_api_leads_with_the_api_key():
    from app.agent.access_planner import credential_ask
    from app.agent.schemas import Intent
    from app.sites.loader import hints_for
    ask = credential_ask("openrouter.ai", hints_for("openrouter.ai"), _rules(), Intent(goal="Check my credits", sites=["openrouter.ai"], needs_login=True))
    assert ask.kinds[0] == "api_key" and ask.reason.startswith("The simplest way to do this on openrouter.ai is with your API key")
    assert "email login" in ask.reason and "Google" in ask.reason                                  # the alternative and the caveat come after


def test_a_site_without_an_api_keeps_the_ordinary_login_card():
    from app.agent.access_planner import credential_ask
    from app.agent.schemas import Intent
    from app.sites.loader import hints_for
    ask = credential_ask("swiggy.com", hints_for("swiggy.com"), _rules(), Intent(goal="order", sites=["swiggy.com"], needs_login=True))
    assert "api_key" not in ask.kinds and "I need a login for swiggy.com" in ask.reason


def test_the_container_health_check_tolerates_a_slow_moment_on_a_small_machine():
    from pathlib import Path
    line = next(l for l in Path("Dockerfile").read_text().splitlines() if l.startswith("HEALTHCHECK"))
    assert "--timeout=15s" in line and "--retries=5" in line and "timeout=12" in line                # was 3 s: a busy 1 GB VM was labelled unhealthy while fine


def test_a_gpt_6_model_is_handled_as_a_thinking_model_by_both_clients():
    """gpt-6-luna rejects temperature != 1 and frequency_penalty: browser-use's list of thinking models stopped at gpt-5, so it would have sent both (HTTP 400 on every step)."""
    from app.browser.executor import THINKING_MODEL_HINTS, Executor
    from app.core.llm import _REASONING
    assert all(_REASONING.match(m) for m in ("gpt-6-luna", "gpt-6.1-sol", "gpt-5.6-luna", "gpt-5-mini", "o3", "o4-mini"))
    assert not any(_REASONING.match(m) for m in ("gpt-4.1", "gpt-4.1-mini", "gpt-4o"))
    llm = Executor(model="gpt-6-luna", provider="openai", api_key="sk-x")._llm()
    assert any(h in llm.model for h in llm.reasoning_models) and "gpt-6" in THINKING_MODEL_HINTS
