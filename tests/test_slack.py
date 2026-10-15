"""The Slack bot: who may use it, that no secret travels through it, and that buttons act only for the person they belong to.
Real database for links and memory; a scripted orchestrator and a recording Slack client."""

import asyncio
import base64
import re
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

import app.core.runtime as rt
from app.agent.graph import Outcome
from app.db.models import Channel, SlackLink, User
from app.slack import blocks
from app.slack.app import start_slack, wire
from app.slack.bot import SlackBot, SlackProgress
from app.slack.links import SlackLinks
from app.slack.router import optional_user, router

T, C, U = "T1", "D1", "U1"
SECRET = "sk-or-v1-" + "a1b2c3d4e5" * 4


class FakeClient:
    def __init__(self):
        self.posts, self.updates, self.uploads, self.views = [], [], [], []
        self.emails, self.profile_error = {}, False      # Slack user id -> the email on their Slack profile

    async def chat_postMessage(self, **kw):
        self.posts.append(kw)
        return {"ts": f"{len(self.posts)}.0"}

    async def chat_update(self, **kw):
        self.updates.append(kw)

    async def files_upload_v2(self, **kw):
        self.uploads.append(kw)

    async def views_open(self, **kw):
        self.views.append(kw)

    async def users_info(self, user):
        if self.profile_error:
            raise RuntimeError("missing_scope")
        return {"user": {"id": user, "profile": {"email": self.emails.get(user, "")}}}

    def everything_said(self) -> str:
        return repr([*self.posts, *self.updates, *self.uploads, *self.views])


class FakeOrch:
    def __init__(self, tmp_path):
        self.script, self.calls, self.accept_code, self.shot, self.gate = [], [], True, None, None
        self.paused = None                       # (task_id, card) the saved checkpoint still holds, as after a restart
        self.api = SimpleNamespace(out_dir=str(tmp_path / "out"))

    async def _next(self, emit):
        await emit("intent", {"goal": "do it"})
        if self.gate:
            await self.gate.wait()
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def start(self, **kw):
        self.calls.append(("start", kw))
        return await self._next(kw["emit"])

    async def resume(self, task_id, value, emit, *, user_id):
        self.calls.append(("resume", task_id, value, user_id))
        return await self._next(emit)

    async def paused_in_thread(self, user_id, thread_id):
        self.calls.append(("paused_in_thread", user_id, thread_id))
        return self.paused

    async def cancel(self, task_id, *, user_id):
        self.calls.append(("cancel", task_id, user_id))

    async def offer_code(self, task_id, user_id, code):
        self.calls.append(("offer_code", task_id, user_id, code))
        return self.accept_code

    async def approval_image(self, task_id, user_id):
        return self.shot


def result(summary="Done", **kw):
    return Outcome("t1", None, {"kind": "result", "verified": True, "confidence": 0.9, "summary": summary, **kw})


def approval(**kw):
    return Outcome("t1", {"type": "approval", "subgoal": "Pay for the order", "reason": "it spends money", "summary": "2 items",
                          "amount": 265.0, "currency": "INR", **kw}, None)


@pytest.fixture
async def world(svc, tmp_path):
    uid = (await svc.tenancy.ensure_user(google_sub="g1", email="a@x.com", name="A")).id
    other = (await svc.tenancy.ensure_user(google_sub="g2", email="b@x.com", name="B")).id
    links = SlackLinks(svc.db)
    client, orch = FakeClient(), FakeOrch(tmp_path)
    bot = SlackBot(client=client, orchestrator=orch, links=links, stm=svc.stm, ltm=svc.ltm, web_url="https://nami.test")
    return SimpleNamespace(bot=bot, client=client, orch=orch, links=links, uid=uid, other=other, svc=svc)


async def link(w, slack_user=U, uid=None):
    await w.links.link(T, slack_user, uid or w.uid)


async def say(w, text, ts=None, user=U):
    await w.bot.on_direct_message(team=T, channel=C, user=user, text=text, ts=ts or f"{len(w.client.posts)}.{abs(hash(text)) % 99999}")


async def click(w, action_id, task_id="t1", user=U):
    await w.bot.on_action(team=T, channel=C, user=user, action_id=action_id, task_id=task_id, message_ts="9.9")


def buttons(msg: dict) -> list[str]:
    return [e["action_id"] for b in msg["blocks"] if b["type"] == "actions" for e in b["elements"]]


# ───────────────────────── who may use it ─────────────────────────

async def test_slack_user_whose_email_matches_a_nami_user_is_linked_with_no_second_step(world):
    w = world
    w.client.emails[U] = "A@X.com"                       # case does not matter
    w.orch.script = [result("Done")]
    await say(w, "check my credits")
    await asyncio.gather(*w.bot._tasks)
    assert [c for c in w.orch.calls if c[0] == "start"][0][1]["user_id"] == w.uid
    assert (await w.links.resolve(T, U)).user_id == w.uid


async def test_unknown_email_is_never_linked_and_creates_no_user(world):
    w = world
    w.client.emails[U] = "stranger@elsewhere.com"
    await say(w, "check my credits")
    assert agent_calls(w) == [] and await w.links.resolve(T, U) is None
    assert "stranger@elsewhere.com" in w.client.posts[0]["text"] and "sign in" in w.client.posts[0]["text"].lower()
    async with w.svc.db.session() as s:
        assert len((await s.execute(select(User))).all()) == 2


@pytest.mark.parametrize("email,broken", [("", False), ("", True), ("not-an-email", False)])
async def test_no_usable_profile_email_means_no_link_and_no_agent(world, email, broken):
    w = world
    w.client.emails[U], w.client.profile_error = email, broken
    await say(w, "check my credits")
    assert agent_calls(w) == [] and await w.links.resolve(T, U) is None
    assert w.client.posts and "users:read.email" in w.client.posts[0]["text"] or "Nami account" in w.client.posts[0]["text"]


async def test_two_slack_accounts_with_one_email_both_map_to_that_user_and_each_other_user_stays_separate(world):
    w = world
    w.client.emails.update({U: "a@x.com", "U2": "b@x.com"})
    assert (await w.links.link_by_email(T, U, "a@x.com")).user_id == w.uid
    assert (await w.links.link_by_email(T, "U2", "b@x.com")).user_id == w.other


async def test_relinking_replaces_the_previous_owner_and_resolve_names_the_org(world):
    await link(world, uid=world.uid)
    await link(world, uid=world.other)
    got = await world.links.resolve(T, U)
    assert got.user_id == world.other and got.org_id
    async with world.svc.db.session() as s:
        assert len((await s.execute(select(SlackLink))).all()) == 1


async def test_unlink_stops_the_bot_acting_for_that_account(world):
    await link(world)
    await say(world, "unlink")
    assert await world.links.resolve(T, U) is None
    await say(world, "hello again")                      # no email on the profile in this test, so it cannot silently re-link
    assert agent_calls(world) == [] and await world.links.resolve(T, U) is None


H = {"X-Requested-With": "nami"}


# ───────────────────────── secrets never travel through Slack ─────────────────────────

@pytest.mark.parametrize("secret", [SECRET, "my password is hunter2hunter2", "otp: 482913"])
async def test_a_pasted_secret_is_refused_and_never_forwarded_or_echoed(world, secret):
    await link(world)
    await say(world, f"log in with {secret}")
    assert agent_calls(world) == []
    said = world.client.everything_said()
    assert "Slack has already stored" in said
    assert secret not in said and "hunter2hunter2" not in said and "482913" not in said


async def test_secrets_are_not_remembered_either(world):
    await link(world)
    await say(world, f"here is my key {SECRET}")
    async with world.svc.db.session() as s:
        assert (await s.execute(select(Channel))).all() == []     # refused before anything was written to memory


async def test_a_typed_code_is_not_accepted_in_chat_and_not_stored(world):
    await link(world)
    world.orch.script = [Outcome("t1", {"type": "ask_user", "purpose": "code", "site": "swiggy.com", "question": "What's the code?"}, None)]
    await say(world, "order food")
    await say(world, "482913")
    assert not [c for c in world.orch.calls if c[0] in ("offer_code", "resume")]
    assert "Enter code" in repr(world.client.posts[-2]) and "482913" not in repr(world.client.posts)


async def test_code_goes_through_the_modal_to_the_orchestrator_and_never_into_a_message(world):
    await link(world)
    world.orch.script = [Outcome("t1", {"type": "ask_user", "purpose": "code", "site": "swiggy.com", "question": "What's the code?"}, None),
                         result("Ordered")]
    await say(world, "order food")
    card = world.client.posts[-1]
    assert buttons(card) == ["nami_code", "nami_code_cancel"]
    await world.bot.open_code_modal(team=T, channel=C, user=U, task_id="t1", trigger_id="trig")
    view = world.client.views[0]["view"]
    assert view["blocks"][0]["element"]["max_length"] == 8
    assert await world.bot.submit_code(team=T, user=U, metadata=view["private_metadata"], value="482913") is None
    await asyncio.gather(*world.bot._tasks)
    assert ("offer_code", "t1", world.uid, "482913") in world.orch.calls
    assert ("resume", "t1", {"code": True}, world.uid) in world.orch.calls
    assert "482913" not in world.client.everything_said()


@pytest.mark.parametrize("value,msg", [("12", "Just the code"), ("abcdef", "Just the code"), ("12 34 56", "Just the code")])
async def test_modal_rejects_things_that_are_not_a_code(world, value, msg):
    await link(world)
    world.orch.script = [Outcome("t1", {"type": "ask_user", "purpose": "code", "question": "code?"}, None)]
    await say(world, "go")
    meta = world.bot.code_modal(C, "t1")["private_metadata"]
    assert msg in await world.bot.submit_code(team=T, user=U, metadata=meta, value=value)
    assert not [c for c in world.orch.calls if c[0] == "offer_code"]


async def test_modal_from_another_slack_account_or_a_stale_task_is_refused(world):
    await link(world)
    await link(world, slack_user="U2", uid=world.other)
    world.orch.script = [Outcome("t1", {"type": "ask_user", "purpose": "code", "question": "code?"}, None)]
    await say(world, "go")
    meta = world.bot.code_modal(C, "t1")["private_metadata"]
    assert await world.bot.submit_code(team=T, user="U2", metadata=meta, value="482913")
    stale = world.bot.code_modal(C, "other-task")["private_metadata"]
    assert await world.bot.submit_code(team=T, user=U, metadata=stale, value="482913")
    assert await world.bot.submit_code(team=T, user=U, metadata="not json", value="482913")
    assert not [c for c in world.orch.calls if c[0] == "offer_code"]


async def test_need_credentials_points_to_the_web_vault_and_has_no_input_here(world):
    await link(world)
    world.orch.script = [Outcome("t1", {"type": "need_credentials", "site": "swiggy.com", "kinds": ["password"], "reason": "I need a login for swiggy.com"}, None),
                         result("ok")]
    await say(world, "order food")
    card = world.client.posts[-1]
    assert "/chainlit/slack/add?token=" in repr(card) and "never through Slack" in repr(card)
    assert "input" not in {b["type"] for b in card["blocks"]}
    await say(world, "done")
    assert ("resume", "t1", {"added": True}, world.uid) in world.orch.calls


# ───────────────────────── approvals ─────────────────────────

async def test_approval_card_then_approve_resumes_as_the_owner_and_the_card_cannot_be_reused(world):
    await link(world)
    world.orch.script = [approval(), result("Ordered")]
    await say(world, "order")
    card = world.client.posts[-1]
    assert buttons(card) == ["nami_approve", "nami_deny"] and "INR 265" in repr(card)
    await click(world, "nami_approve")
    assert ("resume", "t1", {"approved": True}, world.uid) in world.orch.calls
    resumes = [c for c in world.orch.calls if c[0] == "resume"]
    await click(world, "nami_approve")                                  # a second click (or a stale card)
    assert [c for c in world.orch.calls if c[0] == "resume"] == resumes
    assert "no longer active" in repr(world.client.updates[-1])


async def test_the_clicked_card_loses_its_buttons(world):
    await link(world)
    world.orch.script = [approval(), result()]
    await say(world, "order")
    await click(world, "nami_approve")
    edit = next(u for u in world.client.updates if u["ts"] == "9.9")
    assert buttons(edit) == [] and "Approved" in edit["text"]


async def test_deny_sends_false(world):
    await link(world)
    world.orch.script = [approval(), Outcome("t1", None, {"kind": "cancelled"})]
    await say(world, "order")
    await click(world, "nami_deny")
    assert ("resume", "t1", {"approved": False}, world.uid) in world.orch.calls


async def test_a_click_from_an_unlinked_or_different_account_does_nothing(world):
    await link(world)
    world.orch.script = [approval()]
    await say(world, "order")
    await click(world, "nami_approve", user="U-stranger")
    await link(world, slack_user="U2", uid=world.other)
    await click(world, "nami_approve", user="U2")
    assert not [c for c in world.orch.calls if c[0] == "resume"]


async def test_a_wrong_task_id_on_a_button_is_ignored(world):
    await link(world)
    world.orch.script = [approval()]
    await say(world, "order")
    await click(world, "nami_approve", task_id="someone-elses-task")
    assert not [c for c in world.orch.calls if c[0] == "resume"]


async def test_approval_screenshot_is_uploaded_before_the_card(world):
    await link(world)
    world.orch.shot = b"\x89PNG-bytes"
    world.orch.script = [approval()]
    await say(world, "order")
    assert world.client.uploads[0]["content"] == b"\x89PNG-bytes"


async def test_cancel_stops_a_pending_task(world):
    await link(world)
    world.orch.script = [approval()]
    await say(world, "order")
    await say(world, "cancel")
    assert ("cancel", "t1", world.uid) in world.orch.calls
    assert "stopped" in world.client.posts[-1]["text"]


# ───────────────────────── running a task ─────────────────────────

async def test_a_message_starts_a_task_for_the_linked_user_with_progress_and_a_result(world):
    await link(world)
    world.orch.script = [result("You have $4.20 left", facts={"Credits": "$4.20"})]
    await say(world, "check my openrouter credits")
    _, kw = starts(world)[0]
    assert kw["user_id"] == world.uid and kw["text"] == "check my openrouter credits" and kw["org_id"]
    assert "Working on it" in world.client.posts[0]["text"]
    assert "Understood" in repr(world.client.updates[-1]) and "What I did" in repr(world.client.updates[-1])
    final = world.client.posts[-1]
    assert "$4.20 left" in repr(final) and "Credits" in repr(final)
    async with world.svc.db.session() as s:
        assert (await s.scalar(select(Channel.kind))) == "slack"


async def test_a_second_message_while_busy_runs_alongside_as_its_own_task(world):
    await link(world)
    world.orch.gate = asyncio.Event()
    world.orch.script = [result("one"), result("two")]
    first = asyncio.create_task(say(world, "task one", ts="1.1"))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(say(world, "task two", ts="1.2"))
    await asyncio.sleep(0.05)
    starts = [c[1] for c in world.orch.calls if c[0] == "start"]
    assert len(starts) == 2
    assert starts[0]["task_id"] != starts[1]["task_id"] and starts[0]["thread_id"] != starts[1]["thread_id"]
    world.orch.gate.set()
    await asyncio.gather(first, second)


async def test_each_running_task_has_its_own_stop_button_and_stop_ends_only_that_one(world):
    await link(world)
    world.orch.gate = asyncio.Event()
    world.orch.script = [result("one")]
    first = asyncio.create_task(say(world, "task one", ts="1.1"))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(say(world, "task two", ts="1.2"))
    await asyncio.sleep(0.05)
    runs = {e["value"] for m in [*world.client.posts, *world.client.updates] for b in m.get("blocks", []) if b["type"] == "actions"
            for e in b["elements"] if e["action_id"] == "nami_stop"}
    assert len(runs) == 2
    starts = [c[1] for c in world.orch.calls if c[0] == "start"]
    main_run = world.bot.convs[(T, C)].run_id
    await click(world, "nami_stop", task_id=next(r for r in runs if r != main_run))      # stop the second one
    await second
    cancelled = [c[1] for c in world.orch.calls if c[0] == "cancel"]
    assert cancelled == [starts[1]["task_id"]]
    assert world.bot.convs[(T, C)].busy                                                  # the first is still going
    world.orch.gate.set()
    await first


async def test_a_secret_sent_while_busy_is_not_started_as_a_task(world):
    await link(world)
    world.orch.gate = asyncio.Event()
    world.orch.script = [result()]
    first = asyncio.create_task(say(world, "task one", ts="1.1"))
    await asyncio.sleep(0.05)
    await say(world, "my password is hunter2hunter2 and key sk-abcdefghijklmnopqrstuvwx", ts="1.2")
    assert len([c for c in world.orch.calls if c[0] == "start"]) == 1
    world.orch.gate.set()
    await first


async def test_a_crash_shows_a_generic_message_never_the_exception(world):
    await link(world)
    world.orch.script = [RuntimeError(f"boom with {SECRET}")]
    await say(world, "do it")
    assert "Something went wrong" in world.client.posts[-1]["text"] and SECRET not in world.client.everything_said()
    world.orch.script = [result()]
    await say(world, "again")
    assert len([c for c in world.orch.calls if c[0] == "start"]) == 2          # the conversation is not stuck busy


async def test_failed_task_offers_retry_which_reruns_the_same_request(world):
    await link(world)
    world.orch.script = [Outcome("t1", None, {"kind": "failed", "reason": "the site timed out"}), result()]
    await say(world, "get my balance")
    assert buttons(world.client.posts[-1]) == ["nami_retry"]
    await click(world, "nami_retry", task_id="")
    starts = [c for c in world.orch.calls if c[0] == "start"]
    assert len(starts) == 2 and starts[1][1]["text"] == "get my balance"


async def test_clarifying_answer_is_passed_back_as_text(world):
    await link(world)
    world.orch.script = [Outcome("t1", {"type": "ask_user", "purpose": "clarify", "question": "Which city?"}, None), result()]
    await say(world, "find me a flat")
    await say(world, "Pune")
    assert ("resume", "t1", "Pune", world.uid) in world.orch.calls


async def test_remember_command_goes_to_memory_not_the_agent(world):
    await link(world)
    await say(world, "remember that I prefer window seats")
    assert agent_calls(world) == [] and world.client.posts


async def test_takeover_card_offers_no_browser_in_slack(world):
    await link(world)
    world.orch.script = [Outcome("t1", {"type": "takeover", "offer_only": True, "site": "x.com", "reason": "a CAPTCHA"}, None)]
    await say(world, "go")
    card = world.client.posts[-1]
    assert buttons(card) == ["nami_takeover_done", "nami_takeover_cancel"] and "/chainlit/slack/solve?token=" in repr(card)      # a link to the secure window, never a browser in Slack
    bare = blocks.pending_message({"type": "takeover", "site": "x.com", "reason": "a CAPTCHA"}, "t1", "https://nami.test/chainlit")
    assert buttons(bare) == ["nami_takeover_cancel"]                                                                           # without a window link: nothing to hand over


# ───────────────────────── after a restart ─────────────────────────

async def restarted(w):
    """A fresh bot over the same database and the same saved checkpoints: everything in memory is gone."""
    w.bot = SlackBot(client=w.client, orchestrator=w.orch, links=w.links, stm=w.svc.stm, ltm=w.svc.ltm, web_url="https://nami.test")


def agent_calls(w):
    """Everything the agent was asked to do (the read-only pause lookup is not that)."""
    return [c for c in w.orch.calls if c[0] != "paused_in_thread"]


def starts(w):
    return [c for c in w.orch.calls if c[0] == "start"]


async def test_after_a_restart_an_old_approve_button_still_works(world):
    await link(world)
    world.orch.paused = ("t9", {"type": "approval", "subgoal": "Pay", "reason": "it spends money"})
    await restarted(world)
    world.orch.script = [result("Ordered")]
    await click(world, "nami_approve", task_id="t9")
    assert ("resume", "t9", {"approved": True}, world.uid) in world.orch.calls
    assert "Approved" in repr(world.client.updates[0])                       # the old card was closed so it can't be pressed twice


async def test_the_thread_asked_about_is_this_dm_and_this_user(world):
    import hashlib
    await link(world)
    await say(world, "hello there")
    await say(world, "get me something")
    call = next(c for c in world.orch.calls if c[0] == "paused_in_thread")
    assert call[1] == world.uid and call[2] == hashlib.sha256(f"{T}:{C}".encode()).hexdigest()[:32]


async def test_after_a_restart_a_message_shows_the_waiting_card_again_and_runs_nothing(world):
    await link(world)
    world.orch.paused = ("t9", {"type": "approval", "subgoal": "Pay", "reason": "it spends money", "amount": 10.0, "currency": "INR"})
    await restarted(world)
    await say(world, "something else entirely")
    assert starts(world) == [] and not [c for c in world.orch.calls if c[0] == "resume"]
    assert "restarted" in world.client.posts[-2]["text"] and buttons(world.client.posts[-1]) == ["nami_approve", "nami_deny"]
    await click(world, "nami_approve", task_id="t9")                      # and the re-shown card works
    assert ("resume", "t9", {"approved": True}, world.uid) in world.orch.calls


async def test_after_a_restart_a_clarifying_answer_is_passed_on(world):
    await link(world)
    world.orch.paused = ("t9", {"type": "ask_user", "purpose": "clarify", "question": "Which city?"})
    await restarted(world)
    world.orch.script = [result()]
    await say(world, "Pune")
    assert ("resume", "t9", "Pune", world.uid) in world.orch.calls and starts(world) == []


async def test_after_a_restart_cancel_cancels_the_waiting_task(world):
    await link(world)
    world.orch.paused = ("t9", {"type": "approval", "subgoal": "Pay", "reason": "x"})
    await restarted(world)
    await say(world, "cancel")
    assert ("cancel", "t9", world.uid) in world.orch.calls


async def test_after_a_restart_a_button_for_a_different_task_is_stale(world):
    await link(world)
    world.orch.paused = ("t9", {"type": "approval", "subgoal": "Pay", "reason": "x"})
    await restarted(world)
    await click(world, "nami_approve", task_id="t1-old")
    assert not [c for c in world.orch.calls if c[0] == "resume"] and "no longer active" in repr(world.client.updates[-1])


async def test_after_a_restart_the_code_modal_still_opens_and_submits(world):
    await link(world)
    world.orch.paused = ("t9", {"type": "ask_user", "purpose": "code", "question": "code?"})
    await restarted(world)
    world.orch.script = [result()]
    await world.bot.open_code_modal(team=T, channel=C, user=U, task_id="t9", trigger_id="trig", message_ts="3.3")
    meta = world.client.views[0]["view"]["private_metadata"]
    assert await world.bot.submit_code(team=T, user=U, metadata=meta, value="482913") is None
    await asyncio.gather(*world.bot._tasks)
    assert ("resume", "t9", {"code": True}, world.uid) in world.orch.calls and "482913" not in world.client.everything_said()


async def test_a_task_that_crashed_mid_step_is_not_revived_by_a_message(world):
    await link(world)
    world.orch.paused = None                                              # no saved pause: it died while running
    await restarted(world)
    world.orch.script = [result("fresh")]
    await say(world, "new request")
    assert len(starts(world)) == 1 and not [c for c in world.orch.calls if c[0] == "resume"]


async def test_retry_after_a_restart_says_the_request_was_lost_instead_of_doing_nothing(world):
    await link(world)
    await restarted(world)
    await click(world, "nami_retry", task_id="")
    assert starts(world) == [] and "send it again" in repr(world.client.updates[-1])


async def test_a_busy_conversation_is_not_rehydrated(world):
    await link(world)
    world.orch.gate = asyncio.Event()
    world.orch.script = [result("one"), result("two")]
    first = asyncio.create_task(say(world, "task one", ts="1.1"))
    second = None
    try:
        for _ in range(50):
            await asyncio.sleep(0.01)
            if any(c[0] == "start" for c in world.orch.calls):
                break
        world.orch.paused = ("t9", {"type": "approval", "subgoal": "x", "reason": "y"})
        second = asyncio.create_task(say(world, "task two", ts="1.2"))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len([c for c in world.orch.calls if c[0] == "start"]) == 2:
                break

        starts = [c[1] for c in world.orch.calls if c[0] == "start"]
        assert len(starts) == 2
        assert starts[0]["thread_id"] != starts[1]["thread_id"]
        assert len([c for c in world.orch.calls if c[0] == "paused_in_thread"]) == 1
        assert "alongside" in world.client.posts[-2]["text"]
    finally:
        world.orch.gate.set()
        await asyncio.gather(*(t for t in (first, second) if t is not None), return_exceptions=True)


# ───────────────────────── channels and the event filter ─────────────────────────

async def test_mention_in_a_channel_redirects_to_dm_and_runs_nothing(world):
    await world.bot.on_mention(channel="C99", ts="5.5")
    assert agent_calls(world) == [] and world.client.posts[0]["thread_ts"] == "5.5" and "directly" in world.client.posts[0]["text"]


class FakeApp:
    def __init__(self):
        self.handlers = {}
        self.client = FakeClient()

    def event(self, name):
        return lambda f: self.handlers.setdefault(name, f)

    def action(self, _):
        return lambda f: self.handlers.setdefault("action", f)

    def view(self, name):
        return lambda f: self.handlers.setdefault(name, f)


@pytest.mark.parametrize("event", [
    {"channel_type": "channel", "user": "U1", "text": "hi", "channel": "C1", "ts": "1"},       # channel chatter
    {"channel_type": "im", "user": "U1", "text": "hi", "channel": "D1", "ts": "1", "subtype": "message_changed"},
    {"channel_type": "im", "user": "U1", "text": "hi", "channel": "D1", "ts": "1", "bot_id": "B1"},   # a bot, including ourselves
    {"channel_type": "im", "text": "hi", "channel": "D1", "ts": "1"},
])
async def test_only_a_persons_plain_dm_reaches_the_bot(world, event):
    app = FakeApp()
    wire(app, world.bot)
    await app.handlers["message"](event=event, body={"team_id": T})
    await asyncio.gather(*world.bot._tasks)
    assert world.client.posts == [] and world.orch.calls == []


async def test_a_plain_dm_event_is_handled(world):
    app = FakeApp()
    wire(app, world.bot)
    await app.handlers["message"](event={"channel_type": "im", "user": U, "text": "hi", "channel": C, "ts": "1"}, body={"team_id": T})
    await asyncio.gather(*world.bot._tasks)
    assert "Link your Nami account" in world.client.posts[0]["text"]


async def test_redelivered_message_is_handled_once(world):
    await say(world, "hello", ts="7.7")
    await say(world, "hello", ts="7.7")
    assert len(world.client.posts) == 1


async def test_bot_is_off_without_both_tokens():
    assert await start_slack(SimpleNamespace(settings=SimpleNamespace(slack_bot_token="xoxb-x", slack_app_token=None))) is None
    assert await start_slack(SimpleNamespace(settings=SimpleNamespace(slack_bot_token=None, slack_app_token=None))) is None


# ───────────────────────── what Slack is shown ─────────────────────────

def test_page_derived_text_cannot_ping_the_channel_or_inject_links():
    m = blocks.final_message({"kind": "result", "verified": True, "summary": "ok <!channel> <https://evil|click>",
                              "facts": {"<!here>": "<@U123>"}})
    text = repr(m["blocks"])
    assert "<!channel>" not in text and "<!here>" not in text and "<@U123>" not in text and "<https://evil" not in text


def test_secret_shaped_text_in_a_result_is_redacted():
    m = blocks.final_message({"kind": "result", "verified": True, "summary": f"key {SECRET}", "content": f"and {SECRET}"})
    assert SECRET not in repr(m)               # neither the card nor the notification preview text


def test_unverified_results_say_so():
    assert "couldn't fully verify" in repr(blocks.final_message({"kind": "result", "verified": False, "summary": "maybe"}))


def test_long_results_are_split_under_slack_limits():
    m = blocks.final_message({"kind": "result", "verified": True, "summary": "x", "content": "y" * 9000})
    assert all(len(b["text"]["text"]) <= 3000 for b in m["blocks"])


async def test_only_files_nami_generated_are_attached(world, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    mine, secret_file = out / "poem.png", tmp_path / "id_rsa"
    mine.write_bytes(b"png")
    secret_file.write_text("private")
    await world.bot._attach(C, {"media": [{"kind": "image", "path": str(mine)}, {"kind": "image", "path": str(secret_file)},
                                         {"kind": "image", "path": str(out / ".." / "id_rsa")}],
                                "screenshot_b64": base64.b64encode(b"shot").decode()})
    sent = [u.get("file") or u.get("filename") for u in world.client.uploads]
    assert sent == [str(mine.resolve()), "page.png"]


async def test_progress_edits_one_message_and_is_throttled_but_never_loses_lines():
    c, t = FakeClient(), [0.0]
    p = SlackProgress(c, C, min_gap=1.5, clock=lambda: t[0])
    for i in range(10):
        await p.emit("subgoal_start", {"goal": f"step {i}"})
        t[0] += 0.1
    await p.flush()
    assert len(c.posts) == 1 and len(c.updates) <= 2
    assert all(f"step {i}" in repr(c.updates[-1]) for i in range(10)) and "What I did" in repr(c.updates[-1])


async def test_progress_failures_never_fail_the_task():
    class Broken(FakeClient):
        async def chat_postMessage(self, **kw):
            raise RuntimeError("rate limited")
    p = SlackProgress(Broken(), C)
    await p.emit("intent", {"goal": "g"})
    await p.flush()


# ───────────────────────── the sign-in cookie on the link page ─────────────────────────

async def test_link_page_reads_the_chainlit_auth_cookie(monkeypatch):
    """Regression: the page once called Chainlit's dependency with the wrong argument, so everyone looked signed out."""
    import app.slack.router as r
    seen = []

    async def fake_auth(token):
        seen.append(token)
        return SimpleNamespace(metadata={"user_id": "u1"}, identifier="a@x.com")
    monkeypatch.setattr(r, "authenticate_user", fake_auth)
    monkeypatch.setattr(r, "get_token_from_cookies", lambda cookies: cookies.get("access_token"))
    anon = SimpleNamespace(cookies={})
    signed = SimpleNamespace(cookies={"access_token": "jwt-123"})
    assert await r.optional_user(anon) is None and seen == []
    assert (await r.optional_user(signed)).metadata["user_id"] == "u1" and seen == ["jwt-123"]


async def test_link_page_treats_a_rejected_cookie_as_signed_out(monkeypatch):
    import app.slack.router as r
    from fastapi import HTTPException

    async def reject(token):
        raise HTTPException(401, "bad token")
    monkeypatch.setattr(r, "authenticate_user", reject)
    monkeypatch.setattr(r, "get_token_from_cookies", lambda cookies: "expired")
    assert await r.optional_user(SimpleNamespace(cookies={"x": "y"})) is None


# ───────────────────────── the one-time "add it to the vault" page (written, not run) ─────────────────────────

from app.browser.login_probe import LoginShape       # noqa: E402
from app.slack.links import AddRequest                       # noqa: E402
from app.vault.kms import LocalKms                           # noqa: E402
from app.vault.service import VaultService                   # noqa: E402

NEEDS = {"type": "need_credentials", "site": "swiggy.com", "kinds": ["password", "totp"],
         "reason": "swiggy.com texts you a code when you sign in.", "notes": ["swiggy.com texts you a code when you sign in.", "Save your phone number as the username."]}


@pytest.fixture
async def addweb(world, monkeypatch):
    vault = VaultService(world.svc.db, LocalKms())
    monkeypatch.setattr(rt, "_runtime", SimpleNamespace(slack_links=world.links, vault=vault))
    app = FastAPI()
    app.include_router(router)
    who = {"u": world.uid}

    async def user():
        return SimpleNamespace(metadata={"user_id": who["u"]}, identifier="a@x.com") if who["u"] else None
    app.dependency_overrides[optional_user] = user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield SimpleNamespace(c=c, who=who, w=world, vault=vault)


def token_for(w, site="swiggy.com", kinds=("password", "totp"), shape=None, uid=None):
    """A one-time add link; `shape` is what the live look at the sign-in page is pretended to have found (None = could not look)."""
    tok = w.links.begin_add(uid or w.uid, site, list(kinds))
    if shape is not None:
        async def looked():
            return shape
        w.links.watch_shape(tok, looked())
    return tok


PHONE_ONLY = LoginShape(observed=True, identifier="phone", password=False, code_likely=True)
EMAIL_PW = LoginShape(observed=True, identifier="email", password=True, google=True)


async def test_the_credentials_card_carries_a_one_time_add_link_scoped_to_the_site(world):
    await link(world)
    world.orch.script = [Outcome("t1", NEEDS, None)]
    await say(world, "login to swiggy and check my last order")
    card = world.client.posts[-1]
    m = re.search(r"https://nami.test/chainlit/slack/add\?token=([\w-]+)", repr(card))
    assert m and "Add it securely" in repr(card)
    assert world.links.peek_add(m.group(1)) == AddRequest(world.uid, "swiggy.com", ("password", "totp"))


async def test_the_card_does_not_say_the_same_thing_twice():
    m = blocks.pending_message(NEEDS, "t1", "https://nami.test/chainlit", "https://nami.test/chainlit/slack/add?token=x")
    section = m["blocks"][0]["text"]["text"]                                        # (the plain `text` fallback for notifications repeats it by design)
    assert section.count("texts you a code when you sign in") == 1 and "phone number" in section


async def test_add_tokens_expire_and_a_new_one_replaces_the_old_for_the_same_site(world):
    t = [0.0]
    links = SlackLinks(world.svc.db, clock=lambda: t[0])
    old = links.begin_add(world.uid, "swiggy.com", ["password"])
    new = links.begin_add(world.uid, "swiggy.com", ["password"])
    assert links.peek_add(old) is None and links.peek_add(new)
    t[0] = 1801
    assert links.peek_add(new) is None


async def test_add_page_follows_what_the_live_look_found_not_a_per_site_list(addweb):
    page = (await addweb.c.get("/chainlit/slack/add", params={"token": token_for(addweb.w, "any-site.example", shape=PHONE_ONLY)})).text
    assert "Phone number" in page and "name='username'" in page and "asks for your phone number" in page
    assert "name='password'" not in page and "2FA" not in page and "name='key'" not in page


async def test_add_page_asks_for_a_password_only_when_the_page_has_one(addweb):
    page = (await addweb.c.get("/chainlit/slack/add", params={"token": token_for(addweb.w, "other.example", shape=EMAIL_PW)})).text
    assert "name='username'" in page and "name='password'" in page and "type='email'" in page
    assert "Google sign-in" in page and "leave empty" not in page           # a seen password is required, and Google is mentioned


async def test_when_the_page_could_not_be_looked_at_the_form_says_so_and_asks_the_usual_fields(addweb):
    page = (await addweb.c.get("/chainlit/slack/add", params={"token": token_for(addweb.w, "blocked.example")})).text
    assert "open blocked.example" in page and "name='username'" in page and "leave empty if the site only sends a one-time code" in page


async def test_2fa_key_field_only_when_asked_and_a_password_exists(addweb):
    with_2fa = (await addweb.c.get("/chainlit/slack/add", params={"token": token_for(addweb.w, "x.example", ("password", "totp"), EMAIL_PW)})).text
    without = (await addweb.c.get("/chainlit/slack/add", params={"token": token_for(addweb.w, "y.example", ("password",), EMAIL_PW)})).text
    assert "2FA" in with_2fa and "2FA" not in without


async def test_add_page_for_an_api_key_site_asks_only_for_the_key(addweb):
    page = (await addweb.c.get("/chainlit/slack/add", params={"token": token_for(addweb.w, "openrouter.ai", ("api_key",))})).text
    assert "name='key'" in page and "name='username'" not in page and "name='password'" not in page


async def test_a_slow_look_does_not_hold_the_page_forever(addweb):
    async def never():
        await asyncio.sleep(3600)
    tok = addweb.w.links.begin_add(addweb.w.uid, "slow.example", ["password"])
    addweb.w.links.watch_shape(tok, never())
    assert await addweb.w.links.shape_of(tok, timeout=0.05) is None


async def test_add_page_refuses_signed_out_and_wrong_account(addweb):
    tok = token_for(addweb.w)
    addweb.who["u"] = None
    assert "Sign in to Nami first" in (await addweb.c.get("/chainlit/slack/add", params={"token": tok})).text
    addweb.who["u"] = addweb.w.other
    assert "different Nami account" in (await addweb.c.get("/chainlit/slack/add", params={"token": tok})).text
    assert "name='username'" not in (await addweb.c.get("/chainlit/slack/add", params={"token": tok})).text


async def test_saving_stores_a_credential_for_that_site_only_and_never_echoes_it(addweb):
    tok = token_for(addweb.w)
    r = await addweb.c.post("/chainlit/slack/add", json={"token": tok, "kind": "password", "fields": {"username": "9876543210"}}, headers=H)
    assert r.status_code == 200 and r.json() == {"saved": True, "replaced": 0} and "9876543210" not in r.text
    (cred,) = await addweb.vault.list(addweb.w.uid)
    assert cred.kind == "password" and cred.origins == ["swiggy.com"]
    assert (await addweb.c.post("/chainlit/slack/add", json={"token": tok, "kind": "password", "fields": {"username": "x"}}, headers=H)).status_code == 400   # used once


async def test_saving_needs_the_same_origin_header_and_the_right_user(addweb):
    tok = token_for(addweb.w)
    body = {"token": tok, "kind": "password", "fields": {"username": "9876543210"}}
    assert (await addweb.c.post("/chainlit/slack/add", json=body)).status_code == 403
    addweb.who["u"] = addweb.w.other
    assert (await addweb.c.post("/chainlit/slack/add", json=body, headers=H)).status_code == 400
    addweb.who["u"] = None
    assert (await addweb.c.post("/chainlit/slack/add", json=body, headers=H)).status_code == 401
    assert await addweb.vault.list(addweb.w.uid) == [] and await addweb.vault.list(addweb.w.other) == []


@pytest.mark.parametrize("body", [
    {"kind": "card", "fields": {"number": "4111111111111111"}},                           # a kind that was not asked for
    {"kind": "password", "fields": {"username": "u", "origins": "evil.com"}},             # the page cannot choose the site
    {"kind": "password", "fields": {"username": "u", "site": "evil.com"}},
    {"kind": "totp", "fields": {"seed": "JBSWY3DPEHPK3PXP"}},                              # 2FA only rides along with a login
    {"kind": "api_key", "fields": {"key": "abc"}},                                         # api_key was not on this token
])
async def test_the_page_cannot_save_anything_the_token_did_not_allow(addweb, body):
    tok = token_for(addweb.w, "swiggy.com", ("password",))
    r = await addweb.c.post("/chainlit/slack/add", json={"token": tok, **body}, headers=H)
    assert r.status_code == 400 and await addweb.vault.list(addweb.w.uid) == []


async def test_a_bad_2fa_key_is_rejected_cleanly_and_the_token_survives_for_a_retry(addweb):
    tok = token_for(addweb.w, "example.com", ("password", "totp"))
    r = await addweb.c.post("/chainlit/slack/add", headers=H, json={"token": tok, "kind": "password",
                            "fields": {"username": "u", "password": "pw-1234", "twofa": "not a seed"}})
    assert r.status_code == 400 and "pw-1234" not in r.text and addweb.w.links.peek_add(tok)


# ───────────────────────── regression: the add page's script must run, and typed values must never reach a URL (written, not run) ─────────────────────────

def test_page_scripts_are_valid_javascript():
    """Regression: an apostrophe in an f-string once broke the script, the browser fell back to a plain GET form submit, and what the
    user typed ended up in the URL and the access log. Checked with node when it is installed."""
    import shutil
    import subprocess
    import tempfile
    import app.slack.router as r
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    for js in (r._LINK_JS, r._ADD_JS):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(js)
        assert subprocess.run([node, "--check", f.name], capture_output=True).returncode == 0


async def test_if_the_script_fails_the_form_cannot_put_values_in_the_url(addweb):
    page = (await addweb.c.get("/chainlit/slack/add", params={"token": token_for(addweb.w, "x.example", shape=EMAIL_PW)})).text
    assert "method=post" in page and "/chainlit/slack/add/no-script" in page
    assert (await addweb.c.get("/chainlit/slack/add", params={"username": "9876543210", "password": "pw"})).status_code == 200   # no token: just "expired"
    assert await addweb.vault.list(addweb.w.uid) == []


def test_slack_page_query_strings_are_hidden_from_the_access_log():
    import logging
    from app.slack.router import HideQuery
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:5", "GET", "/chainlit/slack/add?username=9876543210&password=hunter2", "1.1", 200), None)
    HideQuery().filter(rec)
    assert "9876543210" not in rec.getMessage() and "hunter2" not in rec.getMessage() and "/chainlit/slack/add?<hidden>" in rec.getMessage()
    other = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d', ("1.2.3.4:5", "GET", "/chainlit/vault/ping?x=1", "1.1", 200), None)
    HideQuery().filter(other)
    assert "x=1" in other.getMessage()                                    # only Slack pages are touched


# ───────────────────────── regression: no link-preview cards (written, not run) ─────────────────────────

async def test_no_message_or_edit_the_bot_sends_asks_slack_for_a_link_preview(world):
    """Regression: step lines contain the URLs the agent visits, and Slack turned each into a preview card with the site's
    title, description and homepage image, on every post and every edit of the progress message."""
    await link(world)
    world.orch.script = [Outcome("t1", {"type": "approval", "subgoal": "Open https://example.com/x", "reason": "r"}, None), result("see https://example.com/y")]
    await say(world, "go to https://example.com")
    await click(world, "nami_approve")
    sent = [*world.client.posts, *world.client.updates]
    assert len(sent) >= 4
    assert all(m.get("unfurl_links") is False and m.get("unfurl_media") is False for m in sent)


async def test_progress_posts_and_edits_both_disable_previews():
    c = FakeClient()
    p = SlackProgress(c, C, min_gap=0)
    await p.emit("step", {"text": "Navigate to https://www.example.com/"})
    await p.emit("step", {"text": "Click login on https://www.example.com/"})
    await p.flush()
    assert c.posts and c.updates
    assert all(m["unfurl_links"] is False and m["unfurl_media"] is False for m in [*c.posts, *c.updates])


# ───────────────────────── stopping a run ─────────────────────────

async def _running(world):
    """Start a request that waits on the gate; returns the task driving it."""
    await link(world)
    world.orch.gate = asyncio.Event()
    t = asyncio.ensure_future(say(world, "do something slow"))
    # Startup touches SQLite before the first progress message. Wait for the
    # actual Stop control instead of assuming a fixed number of event-loop
    # ticks is enough on every machine.
    for _ in range(300):
        if _stop_value(world):
            return t
        await asyncio.sleep(0.01)
    world.orch.gate.set()
    await asyncio.gather(t, return_exceptions=True)
    raise AssertionError("running task did not publish its Stop button")


def _stop_value(world):
    msgs = [*world.client.posts, *world.client.updates]
    for m in reversed(msgs):
        for b in m.get("blocks", []):
            for e in b.get("elements", []) if b["type"] == "actions" else []:
                if e["action_id"] == "nami_stop":
                    return e["value"]
    return None


def test_progress_has_stop_only_while_working():
    assert "nami_stop" in buttons(blocks.progress_message(["a"], True, stop_value="r1"))
    assert "nami_stop" not in buttons(blocks.progress_message(["a"], False, stop_value="r1"))


async def test_stop_button_cancels_the_run_and_closes_the_task(world):
    t = await _running(world)
    run = _stop_value(world)
    assert run
    await click(world, "nami_stop", task_id=run)
    await t
    started = next(c[1] for c in world.orch.calls if c[0] == "start")
    assert ("cancel", started["task_id"], world.uid) in world.orch.calls
    assert "Stopped" in world.client.everything_said()
    assert "nami_stop" not in [a for m in world.client.updates[-1:] for a in buttons(m)]


async def test_a_stale_stop_click_is_ignored(world):
    await link(world)
    await click(world, "nami_stop", task_id="old-run")
    assert not [c for c in world.orch.calls if c[0] == "cancel"]


async def test_saying_cancel_mid_run_stops_it(world):
    t = await _running(world)
    await say(world, "cancel")
    await t
    assert [c for c in world.orch.calls if c[0] == "cancel"]


async def test_someone_else_cannot_stop_my_run(world):
    t = await _running(world)
    run = _stop_value(world)
    await click(world, "nami_stop", task_id=run, user="U_OTHER")
    assert not [c for c in world.orch.calls if c[0] == "cancel"]
    world.orch.gate.set()
    world.orch.script.append(result())
    await t


def test_a_clarifying_question_card_can_be_cancelled():
    msg = blocks.pending_message({"type": "ask_user", "question": "Which one?"}, "t1", "https://x")
    assert "nami_ask_cancel" in buttons(msg)
