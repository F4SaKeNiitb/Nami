"""KernelProvider against a fake SDK client: right parameters, profile saved, no raw ids leaked."""

from types import SimpleNamespace

import httpx
import pytest
from kernel import BadRequestError

from app.browser.kernel_provider import KernelProvider
from app.browser.provider import ProfileBusy, browser_session


class FakeProfiles:
    def __init__(self):
        self.created = []

    async def create(self, name):
        self.created.append(name)


class FakeBrowsers:
    def __init__(self, profiles=None):
        self.created, self.deleted, self.fail_create = [], [], False
        self.profiles, self.known = profiles, None            # known=None: every profile exists

    async def create(self, **kw):
        if self.known is not None and kw["profile"]["name"] not in self.known and kw["profile"]["name"] not in self.profiles.created:
            raise BadRequestError("profile not found", response=httpx.Response(400, request=httpx.Request("POST", "http://k")),
                                  body={"code": "invalid_request", "message": "profile not found"})
        if self.fail_create:
            raise RuntimeError("kernel 503")
        self.created.append(kw)
        return SimpleNamespace(session_id=f"k{len(self.created)}", cdp_ws_url="wss://cdp/x",
                               browser_live_view_url="https://live/x")

    async def delete_by_id(self, sid):
        self.deleted.append(sid)


@pytest.fixture
def fake():
    profiles = FakeProfiles()
    return SimpleNamespace(browsers=FakeBrowsers(profiles), profiles=profiles)


async def test_open_requests_headless_stealth_profile_that_saves_changes(fake):
    p = KernelProvider(fake, headless=True)
    s = await p.open("alice@example.com")
    kw = fake.browsers.created[0]
    assert kw["headless"] is True and kw["stealth"] is True
    assert kw["profile"]["save_changes"] is True and kw["timeout_seconds"] > 0
    assert (s.cdp_url, s.live_view_url) == ("wss://cdp/x", "https://live/x")


async def test_profile_name_is_stable_per_user_and_hides_the_user_id(fake):
    name = KernelProvider.profile_name("alice@example.com")
    assert name == KernelProvider.profile_name("alice@example.com")
    assert name != KernelProvider.profile_name("bob@example.com")
    assert "alice" not in name and "example" not in name


async def test_close_deletes_the_browser_which_saves_the_profile(fake):
    p = KernelProvider(fake)
    with pytest.raises(ValueError):
        async with browser_session(p, "alice"):
            raise ValueError("task failed")
    assert fake.browsers.deleted == ["k1"]          # still deleted (profile written) after an error


async def test_one_writer_per_profile(fake):
    p = KernelProvider(fake)
    await p.open("alice")
    with pytest.raises(ProfileBusy):
        await p.open("alice")
    assert len(fake.browsers.created) == 1          # no second browser was even requested


async def test_kernel_outage_surfaces_and_frees_the_lock(fake):
    fake.browsers.fail_create = True
    p = KernelProvider(fake)
    with pytest.raises(RuntimeError):
        await p.open("alice")
    assert not p.is_open("alice")


async def test_default_is_headless_whatever_the_caller_asks_for(fake):
    await KernelProvider(fake).open("alice", headless=False)
    await KernelProvider(fake).open("bob", headless=True)
    assert [c["headless"] for c in fake.browsers.created] == [True, True]


async def test_headful_only_when_configured_and_then_even_if_headless_is_requested(fake):
    """Escape hatch for sites that refuse Kernel's headless browser (seen: Swiggy, Blinkit): NAMI_KERNEL_HEADLESS=false."""
    await KernelProvider(fake, headless=False).open("alice", headless=True)
    assert fake.browsers.created[0]["headless"] is False


async def test_first_use_creates_the_missing_profile_then_opens(fake):
    fake.browsers.known = set()
    p = KernelProvider(fake)
    await p.open("alice")
    assert fake.profiles.created == [KernelProvider.profile_name("alice")]
    assert len(fake.browsers.created) == 1


async def test_existing_profile_is_not_recreated(fake):
    fake.browsers.known = {KernelProvider.profile_name("alice")}
    await KernelProvider(fake).open("alice")
    assert fake.profiles.created == []


async def test_other_bad_requests_are_not_swallowed(fake):
    async def boom(**kw):
        raise BadRequestError("invalid timeout", response=httpx.Response(400, request=httpx.Request("POST", "http://k")), body=None)
    fake.browsers.create = boom
    p = KernelProvider(fake)
    with pytest.raises(BadRequestError):
        await p.open("alice")
    assert fake.profiles.created == [] and not p.is_open("alice")


async def test_runtime_passes_the_settings_through_and_kernels_idle_timeout_is_a_safety_net_behind_ours():
    from app.core.config import Settings
    from app.core.runtime import build_provider
    d = build_provider(Settings(browser_provider="kernel", kernel_api_key="k"))
    assert d.headless is True and d.timeout_seconds == 600 + 300           # headless by default; our reaper closes at 600s, before Kernel's own timeout
    assert build_provider(Settings(browser_provider="kernel", kernel_api_key="k", kernel_headless=False)).headless is False
    assert build_provider(Settings(browser_provider="kernel", kernel_api_key="k", browser_idle_seconds=120)).timeout_seconds == 420


def _denied():
    from kernel import PermissionDeniedError
    return PermissionDeniedError("insufficient_plan", response=httpx.Response(403, request=httpx.Request("POST", "http://k")),
                                 body={"code": "insufficient_plan"})


async def test_region_is_sent_when_set_and_left_out_when_not(fake):
    await KernelProvider(fake, region="ap-southeast").open("alice")
    await KernelProvider(fake).open("bob")
    assert fake.browsers.created[0]["region"] == "ap-southeast" and "region" not in fake.browsers.created[1]


async def test_a_plan_that_cannot_choose_a_region_falls_back_instead_of_failing(fake):
    real_create = fake.browsers.create

    async def create(**kw):
        if "region" in kw:
            raise _denied()
        return await real_create(**kw)
    fake.browsers.create = create
    p = KernelProvider(fake, region="ap-southeast")
    await p.open("alice")
    assert "region" not in fake.browsers.created[0] and p.region is None          # opened without it, and won't keep asking


async def test_other_permission_errors_are_not_swallowed(fake):
    from kernel import PermissionDeniedError

    async def create(**kw):
        raise PermissionDeniedError("forbidden", response=httpx.Response(403, request=httpx.Request("POST", "http://k")), body={"code": "forbidden"})
    fake.browsers.create = create
    with pytest.raises(PermissionDeniedError):
        await KernelProvider(fake, region="ap-southeast").open("alice")
