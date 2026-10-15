import pytest

from app.tenancy.service import PermissionDenied


async def test_user_cannot_loosen_firm_hard_spend_limit(svc, firm):
    org, admin, member = firm
    await svc.rules.add_rule(actor_id=admin.id, org_id=org.id, kind="policy", key="spend_limit",
                             value={"INR": 2000}, enforcement="hard")
    await svc.rules.add_rule(actor_id=member.id, org_id=org.id, user_id=member.id, kind="policy",
                             key="spend_limit", value={"INR": 5000})
    eff = await svc.rules.resolve(org_id=org.id, user_id=member.id)
    assert eff.policies["spend_limit"] == {"INR": 2000}
    assert eff.within_spend(1500, "inr") and not eff.within_spend(2500, "INR")


async def test_user_can_tighten_and_override_soft_defaults(svc, firm):
    org, admin, member = firm
    await svc.rules.add_rule(actor_id=admin.id, org_id=org.id, kind="policy", key="spend_limit",
                             value={"INR": 2000}, enforcement="hard")
    await svc.rules.add_rule(actor_id=admin.id, org_id=org.id, kind="preference", key="diet", value="any")
    await svc.rules.add_rule(actor_id=member.id, org_id=org.id, user_id=member.id, kind="policy",
                             key="spend_limit", value={"INR": 800})
    await svc.rules.add_rule(actor_id=member.id, org_id=org.id, user_id=member.id, kind="preference",
                             key="diet", value="veg")
    eff = await svc.rules.resolve(org_id=org.id, user_id=member.id)
    assert eff.policies["spend_limit"] == {"INR": 800}
    assert eff.preferences["diet"] == "veg"
    # the admin isn't affected by the member's own rules
    admin_eff = await svc.rules.resolve(org_id=org.id, user_id=admin.id)
    assert admin_eff.preferences["diet"] == "any"


async def test_domain_lists_and_site_specific_rules(svc, firm):
    org, admin, member = firm
    await svc.rules.add_rule(actor_id=admin.id, org_id=org.id, kind="policy", key="domain_deny",
                             value=["Gambling.com"], enforcement="hard")
    await svc.rules.add_rule(actor_id=member.id, org_id=org.id, user_id=member.id, kind="policy",
                             key="domain_deny", value=["reddit.com"])
    await svc.rules.add_rule(actor_id=member.id, org_id=org.id, user_id=member.id, kind="preference",
                             key="address", value="Office")
    await svc.rules.add_rule(actor_id=member.id, org_id=org.id, user_id=member.id, kind="preference",
                             key="address", value="Home", site="swiggy.com")
    general = await svc.rules.resolve(org_id=org.id, user_id=member.id)
    swiggy = await svc.rules.resolve(org_id=org.id, user_id=member.id, site="Swiggy.com")
    assert general.policies["domain_deny"] == ["gambling.com", "reddit.com"]
    assert not general.domain_allowed("gambling.com") and general.domain_allowed("vercel.com")
    assert general.preferences["address"] == "Office" and swiggy.preferences["address"] == "Home"


async def test_baseline_approvals_cannot_be_removed(svc, firm):
    org, admin, member = firm
    await svc.rules.add_rule(actor_id=admin.id, org_id=org.id, kind="policy", key="require_approval",
                             value=["deploy"], enforcement="hard")
    eff = await svc.rules.resolve(org_id=org.id, user_id=member.id)
    assert eff.needs_approval("purchase") and eff.needs_approval("deploy")


async def test_permissions(svc, firm):
    org, admin, member = firm
    with pytest.raises(PermissionDenied):
        await svc.rules.add_rule(actor_id=member.id, org_id=org.id, kind="policy",
                                 key="spend_limit", value={"INR": 1})
    with pytest.raises(PermissionDenied):
        await svc.rules.add_rule(actor_id=member.id, org_id=org.id, user_id=admin.id, kind="preference",
                                 key="diet", value="veg")
    with pytest.raises(ValueError):
        await svc.rules.add_rule(actor_id=member.id, org_id=org.id, user_id=member.id, kind="policy",
                                 key="spend_limit", value={"INR": 1}, enforcement="hard")
    with pytest.raises(ValueError):
        await svc.rules.add_rule(actor_id=admin.id, org_id=org.id, kind="policy", key="teleport", value=True)


async def test_cache_invalidated_by_version_bump(svc, firm, redis):
    org, admin, member = firm
    r = await svc.rules.add_rule(actor_id=admin.id, org_id=org.id, kind="instruction", key="gst",
                                 value="Always add the firm GST number on invoices.", enforcement="hard")
    first = await svc.rules.resolve(org_id=org.id, user_id=member.id)
    assert await redis.exists(f"nami:rules:{first.version}")
    assert "[Firm policy (must follow)] Always add the firm GST" in first.prompt_block()

    await svc.rules.set_enabled(actor_id=admin.id, rule_id=r.id, enabled=False)
    second = await svc.rules.resolve(org_id=org.id, user_id=member.id)
    assert second.version != first.version and second.instructions == []


async def test_rules_work_without_redis(make_services):
    svc = await make_services(use_redis=False)
    a = await svc.tenancy.ensure_user(google_sub="x", email="x@y.z")
    await svc.rules.add_rule(actor_id=a.id, org_id=a.default_org_id, user_id=a.id, kind="preference",
                             key="lang", value="en")
    assert (await svc.rules.resolve(org_id=a.default_org_id, user_id=a.id)).preferences == {"lang": "en"}
