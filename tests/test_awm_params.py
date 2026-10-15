"""Parameterised workflow steps: the user's wording becomes a slot when saved and is refilled from the next request."""

from cryptography.hazmat.primitives.asymmetric import ec

from app.skills import params
from app.skills.schemas import SkillContent
from app.skills.service import SkillService
from app.skills.signing import Signer


def test_constraint_found_in_a_step_becomes_a_slot():
    steps, found = params.parameterise(["Open the menu", "Filter to Under ₹300 only"], ["under ₹300", "vegetarian"])
    assert steps[1] == "Filter to {constraint_1} only" and found == {"constraint_1": "under ₹300"}


def test_constraint_not_in_any_step_is_left_alone():
    steps, found = params.parameterise(["Open the menu"], ["vegetarian"])
    assert steps == ["Open the menu"] and found == {}


def test_tiny_huge_or_brace_constraints_are_ignored():
    assert params.parameterise(["a ab abc"], ["ab"])[1] == {}
    assert params.parameterise(["x" * 80], ["x" * 80])[1] == {}
    assert params.parameterise(["use {x} here"], ["{x}"])[1] == {}


def test_fill_uses_the_new_requests_wording_never_the_saved_example():
    text = params.fill("Filter to {constraint_1} and {constraint_2}", {"constraint_1": "under ₹300"}, ["under ₹150"])
    assert text == f"Filter to under ₹150 and {params.NEUTRAL}"


def test_fill_with_no_constraints_is_neutral():
    assert params.fill("Filter to {constraint_1}", {"constraint_1": "x"}, []) == f"Filter to {params.NEUTRAL}"


async def test_saved_skill_is_parameterised_and_loaded_with_the_new_constraint(svc):
    u = await svc.tenancy.ensure_user(google_sub="g1", email="a@x.com", name="A")
    org = await svc.tenancy.create_org(name="Acme", owner_id=u.id)
    sk = SkillService(svc.db, Signer(ec.generate_private_key(ec.SECP256R1())))
    c = SkillContent(site="acme.com", intent="order lunch", steps=["Open Menu", "Filter to under ₹300"])
    saved = await sk.propose(org_id=org.id, user_id=u.id, content=c, observed_domains=["acme.com"], plan_domains=["acme.com"],
                             constraints=["under ₹300"])
    assert saved.content["steps"][1] == "Filter to {constraint_1}" and saved.content["params"] == {"constraint_1": "under ₹300"}
    assert saved.status == "approved"
    got = await sk.load_for(org_id=org.id, user_id=u.id, site="acme.com", goal="order lunch", plan_domains=["acme.com"],
                            constraints=["under ₹150"])
    assert "under ₹150" in got[0].text and "₹300" not in got[0].text
