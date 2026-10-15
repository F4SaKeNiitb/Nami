"""Attaching to Chrome over CDP is retried: seen live, the first attach right after a sign-in can fail and the next works."""

from types import SimpleNamespace

import pytest

from app.browser import cdp


class FakeChromium:
    def __init__(self, failures, browser=None):
        self.failures, self.calls = list(failures), 0
        self.browser = browser or SimpleNamespace(contexts=["ctx"], close=self._close)
        self.closed = 0

    async def _close(self):
        self.closed += 1

    async def connect_over_cdp(self, url):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return self.browser


@pytest.fixture
def chromium(monkeypatch):
    monkeypatch.setattr(cdp, "ATTACH_BACKOFF", 0)

    def make(failures):
        fake = FakeChromium(failures)

        async def pw():
            return SimpleNamespace(chromium=fake)
        monkeypatch.setattr(cdp, "_playwright", pw)
        return fake
    return make


CTX_ERR = RuntimeError("BrowserType.connect_over_cdp: Protocol error (Browser.setDownloadBehavior): Browser context management is not supported.")


async def test_a_transient_attach_failure_is_retried_and_then_works(chromium):
    fake = chromium([CTX_ERR, CTX_ERR])
    async with cdp.connect("http://127.0.0.1:1") as browser:
        assert browser.contexts == ["ctx"]
    assert fake.calls == 3 and fake.closed == 1                 # disconnected once when the block ended


async def test_a_good_first_attach_is_not_retried(chromium):
    fake = chromium([])
    async with cdp.connect("http://127.0.0.1:1"):
        pass
    assert fake.calls == 1


async def test_a_persistent_failure_is_raised_after_a_bounded_number_of_attempts(chromium):
    fake = chromium([CTX_ERR] * 50)
    with pytest.raises(RuntimeError, match="not supported"):
        async with cdp.connect("http://127.0.0.1:1"):
            pass
    assert fake.calls == cdp.ATTACH_ATTEMPTS


async def test_the_default_context_helper_uses_the_retrying_attach(chromium):
    fake = chromium([CTX_ERR])
    async with cdp.default_context("http://127.0.0.1:1") as ctx:
        assert ctx == "ctx"
    assert fake.calls == 2


async def test_an_exception_inside_the_block_still_disconnects(chromium):
    fake = chromium([])
    with pytest.raises(ValueError):
        async with cdp.connect("http://127.0.0.1:1"):
            raise ValueError("boom")
    assert fake.closed == 1
