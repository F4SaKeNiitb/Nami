"""The agent moves through a site by clicking, not by typing guessed addresses. Written, not run (test pause).

Found live: signing in to Swiggy, the agent "tried common login URL patterns" and opened /auth/login instead of clicking Log in."""

import pytest
from browser_use import Tools
from browser_use.tools.views import NavigateAction

from app.browser.navigation import GUESSED, NAVIGATION_RULE, install, navigation_decision, urls_in

SITE = ["swiggy.com"]


def decide(url, here="https://www.swiggy.com/", mentioned=()):
    return navigation_decision(url, current_url=here, allowed_domains=SITE, mentioned=list(mentioned))


@pytest.mark.parametrize("url", ["https://www.swiggy.com", "https://www.swiggy.com/", "https://swiggy.com", "http://swiggy.com/"])
def test_a_sites_home_page_is_always_a_fine_place_to_start(url):
    assert decide(url).allow


@pytest.mark.parametrize("url", ["https://www.swiggy.com/auth/login", "https://www.swiggy.com/login", "https://www.swiggy.com/orders",
                                 "https://www.swiggy.com/my-account/orders?x=1", "https://www.swiggy.com/?redirect=/login"])
def test_guessed_paths_are_refused_with_an_instruction_to_click(url):
    d = decide(url)
    assert not d.allow and d.reason == GUESSED and "click" in d.reason


def test_reloading_the_current_page_is_allowed():
    assert decide("https://www.swiggy.com/restaurants/", here="https://www.swiggy.com/restaurants").allow
    assert decide("https://swiggy.com/restaurants#top", here="https://www.swiggy.com/restaurants").allow


def test_an_address_the_user_or_planner_wrote_is_allowed():
    given = urls_in("Open https://www.swiggy.com/my-account and read the last order.")
    assert decide("https://www.swiggy.com/my-account", mentioned=given).allow
    assert not decide("https://www.swiggy.com/my-account/addresses", mentioned=given).allow          # a neighbour of it is a guess


def test_a_trailing_full_stop_in_the_text_is_not_part_of_the_address():
    assert urls_in("Go to https://www.swiggy.com/my-account.") == ["https://www.swiggy.com/my-account"]


@pytest.mark.parametrize("url", ["https://evil.com/", "https://swiggy.com.evil.co.uk/", "javascript:alert(1)", "file:///etc/passwd", "", "swiggy.com/x"])
def test_other_sites_and_non_web_addresses_are_refused_even_at_the_root(url):
    assert not decide(url).allow


def test_the_rule_is_in_the_prompt_text():
    assert "never invent or guess" in NAVIGATION_RULE.lower() and "click" in NAVIGATION_RULE.lower()


async def test_the_agents_navigate_action_is_wrapped_so_a_guess_never_reaches_the_browser():
    tools, opened, blocked = Tools(), [], []

    async def original(params, **ctx):
        opened.append(params.url)
        return "navigated"
    tools.registry.registry.actions["navigate"].function = original

    async def on_block(url):
        blocked.append(url)
    assert install(tools, allowed_domains=SITE, mentioned=[], on_block=on_block)
    nav = tools.registry.registry.actions["navigate"].function
    refused = await nav(params=NavigateAction(url="https://www.swiggy.com/auth/login"), page_url="https://www.swiggy.com/")
    assert refused.error == GUESSED and opened == [] and blocked == ["https://www.swiggy.com/auth/login"]
    assert await nav(params=NavigateAction(url="https://www.swiggy.com/"), page_url="https://www.swiggy.com/x") == "navigated"
    assert opened == ["https://www.swiggy.com/"]


async def test_a_new_tab_request_is_held_to_the_same_rule():
    tools, opened = Tools(), []

    async def original(params, **ctx):
        opened.append(params.url)
    tools.registry.registry.actions["navigate"].function = original

    async def on_block(url):
        pass
    install(tools, allowed_domains=SITE, mentioned=[], on_block=on_block)
    nav = tools.registry.registry.actions["navigate"].function
    r = await nav(params=NavigateAction(url="https://www.swiggy.com/secret-admin", new_tab=True), page_url="")
    assert r.error and opened == []


def test_install_reports_when_there_is_no_navigate_action():
    class Bare:
        class registry:
            class registry:
                actions = {}
    assert install(Bare(), allowed_domains=SITE, mentioned=[], on_block=None) is False


def test_the_planner_is_told_not_to_write_addresses_into_goals():
    from app.agent.planner import SYSTEM
    assert "Never write a web address or path in a goal" in SYSTEM


def test_progress_line_exists_for_a_blocked_navigation():
    from app.progress import progress_lines
    assert progress_lines("navigation_blocked", {"url": "x"})
