"""Explicit memory commands: the only way text enters long-term memory, and how recalled memory reaches the models.

Safety first: secrets are refused, tasks are not mistaken for facts, forget only touches the user's own memories,
and recalled memory is shown to models as labelled, redacted data."""
import pytest

from app.agent.intent import parse_intent
from app.agent.planner import make_plan
from app.agent.schemas import AccessPlan, Intent, Plan, Subgoal
from app.memory.commands import (MAX_MEMORY_CHARS, MemoryCommand, memory_block, parse_memory_command,
                                 run_memory_command)


class Recorder:
    def __init__(self, out):
        self.out, self.seen = out, []

    async def json(self, *, system, user, schema, images=None, model=None, max_tokens=0):
        self.seen.append(user)
        return self.out


# ── safety ──

async def test_a_secret_is_never_stored_as_a_memory(svc, firm):
    org, _, member = firm
    cmd = parse_memory_command("remember that my openrouter key is sk-or-v1-" + "a" * 48)
    assert cmd and cmd.action == "remember"
    reply = await run_memory_command(svc.ltm, cmd, org_id=org.id, user_id=member.id, thread_id=None)
    assert "secret" in reply.lower()
    assert await svc.ltm.list_visible(org_id=org.id, user_id=member.id) == []


@pytest.mark.parametrize("text", [
    "remember to book a table for 8 tonight",      # a task, not a fact
    "order my usual from swiggy",
    "what did I order last time?",
    "",
    "remember that " + "x" * (MAX_MEMORY_CHARS + 50),   # too long to be a memory note
])
def test_tasks_and_ordinary_requests_are_not_memory_commands(text):
    assert parse_memory_command(text) is None


async def test_forget_never_deletes_an_org_memory_or_a_weak_match(svc, firm):
    org, admin, member = firm
    await svc.ltm.remember(org_id=org.id, scope="org", content="Acme orders lunch from swiggy on Fridays", created_by=admin.id)
    await svc.ltm.remember(org_id=org.id, scope="user", user_id=member.id, content="I am vegetarian", kind="preference")
    reply = await run_memory_command(svc.ltm, MemoryCommand("forget", "Acme orders lunch from swiggy on Fridays"),
                                     org_id=org.id, user_id=member.id, thread_id=None)
    assert "nothing was deleted" in reply
    reply = await run_memory_command(svc.ltm, MemoryCommand("forget", "my favourite colour is blue"),
                                     org_id=org.id, user_id=member.id, thread_id=None)
    assert "nothing was deleted" in reply
    left = {m.content for m in await svc.ltm.list_visible(org_id=org.id, user_id=member.id)}
    assert left == {"Acme orders lunch from swiggy on Fridays", "I am vegetarian"}


async def test_forget_everything_only_removes_this_users_memories(svc, firm):
    org, admin, member = firm
    await svc.ltm.remember(org_id=org.id, scope="org", content="Acme orders lunch from swiggy on Fridays", created_by=admin.id)
    await svc.ltm.remember(org_id=org.id, scope="user", user_id=admin.id, content="admin likes window seats")
    await run_memory_command(svc.ltm, MemoryCommand("remember", "I am vegetarian"), org_id=org.id, user_id=member.id, thread_id=None)
    reply = await run_memory_command(svc.ltm, MemoryCommand("forget_all"), org_id=org.id, user_id=member.id, thread_id=None)
    assert "deleted 1 saved memory" in reply
    assert {m.content for m in await svc.ltm.list_visible(org_id=org.id, user_id=admin.id)} == {
        "Acme orders lunch from swiggy on Fridays", "admin likes window seats"}


def test_memory_block_labels_memories_as_the_users_own_words_and_rules_win():
    block = memory_block(["I am vegetarian"])
    assert "their own words" in block and "rules take precedence" in block and "- I am vegetarian" in block
    assert memory_block([]) == ""


async def test_recalled_memory_reaches_the_intent_step_redacted_and_labelled():
    rec = Recorder(Intent(goal="order lunch"))
    leaked = "sk-or-v1-" + "b" * 48
    await parse_intent(rec, "order lunch", memory=memory_block(["I am vegetarian", f"old note {leaked}"]))
    assert "I am vegetarian" in rec.seen[0] and "their own words" in rec.seen[0]
    assert leaked not in rec.seen[0]


async def test_no_memory_means_the_intent_prompt_is_unchanged():
    rec = Recorder(Intent(goal="x"))
    await parse_intent(rec, "do x")
    assert "remember" not in rec.seen[0].lower()


async def test_planner_sees_memory_as_data_next_to_the_rules():
    rec = Recorder(Plan(subgoals=[Subgoal(id=1, goal="g", success_criterion="c")]))
    access = AccessPlan(allowed_domains=["swiggy.com"], login="session")
    await make_plan(rec, Intent(goal="order lunch"), access, rules_block="spend_limit INR 2000",
                    memory=memory_block(["I am vegetarian"]))
    assert "I am vegetarian" in rec.seen[0] and "spend_limit INR 2000" in rec.seen[0]


# ── behaviour ──

@pytest.mark.parametrize("text,action,body", [
    ("remember that I'm vegetarian", "remember", "I'm vegetarian"),
    ("Remember: deliver to the office.", "remember", "deliver to the office"),
    ("please keep in mind that I pay with UPI", "remember", "I pay with UPI"),
    ("what do you remember about me?", "list", ""),
    ("show my memories", "list", ""),
    ("forget that I'm vegetarian", "forget", "I'm vegetarian"),
    ("forget everything", "forget_all", ""),
])
def test_commands_are_parsed(text, action, body):
    assert parse_memory_command(text) == MemoryCommand(action, body)


async def test_remember_list_and_forget_round_trip(svc, firm):
    org, _, member = firm
    say = lambda c: run_memory_command(svc.ltm, parse_memory_command(c), org_id=org.id, user_id=member.id, thread_id=None)
    assert "I'll remember" in await say("remember that I am vegetarian")
    listed = await say("what do you remember?")
    assert "- I am vegetarian" in listed
    assert "Forgotten" in await say("forget that I am vegetarian")
    assert "anything saved" in await say("what do you remember?")
