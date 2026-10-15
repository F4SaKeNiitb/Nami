"""End-to-end smoke test of the storage layer against the configured SQLite + Redis.

    redis-server --port 6390 --save '' --appendonly no --daemonize yes
    alembic upgrade head
    NAMI_REDIS_URL=redis://localhost:6390/0 python -m scripts.smoke_storage
"""

import asyncio
import time

from app.cache.llm_cache import LLMCacheScope
from app.core.config import get_settings
from app.core.container import build_services


async def main() -> None:
    settings = get_settings()
    svc = build_services(settings)
    assert svc.redis is not None, "set NAMI_REDIS_URL"
    print("redis:", (await svc.redis.info("server"))["redis_version"], "| db:", settings.database_url)
    print("semantic backend:", type(svc.llm_cache.semantic.index).__name__,
          "| embedder:", svc.ltm.embedder.model_name)

    tag = str(int(time.time()))
    admin = await svc.tenancy.ensure_user(google_sub=f"smoke-a-{tag}", email=f"a{tag}@acme.in", name="Asha")
    ravi = await svc.tenancy.ensure_user(google_sub=f"smoke-r-{tag}", email=f"r{tag}@acme.in", name="Ravi")
    org = await svc.tenancy.create_org(name=f"Acme {tag}", owner_id=admin.id)
    await svc.tenancy.add_member(org_id=org.id, user_id=ravi.id, role="member", actor_id=admin.id)

    await svc.rules.add_rule(actor_id=admin.id, org_id=org.id, kind="policy", key="spend_limit",
                             value={"INR": 2000}, enforcement="hard")
    await svc.rules.add_rule(actor_id=ravi.id, org_id=org.id, user_id=ravi.id, kind="preference",
                             key="address", value="Home", site="swiggy.com")
    t0 = time.perf_counter(); eff = await svc.rules.resolve(org_id=org.id, user_id=ravi.id, site="swiggy.com")
    t1 = time.perf_counter(); await svc.rules.resolve(org_id=org.id, user_id=ravi.id, site="swiggy.com")
    t2 = time.perf_counter()
    print(f"\nrules (cold {1000*(t1-t0):.1f} ms, cached {1000*(t2-t1):.1f} ms):\n{eff.prompt_block()}")

    ch = await svc.stm.ensure_channel(org_id=org.id, kind="web", external_id=f"web-{ravi.id}")
    th = await svc.stm.open_thread(channel_id=ch.id, org_id=org.id, user_id=ravi.id)
    for i, (role, text) in enumerate([("user", "Repeat my last Swiggy order"),
                                      ("assistant", "Found it: 2x paneer biryani, ₹486. Place it?"),
                                      ("user", "yes, and my otp is 482913")]):
        await svc.stm.append(th.id, role, text, ravi.id if role == "user" else None)
    ctx = await svc.stm.context(th.id)
    print("\nshort-term (from Redis):", [(t.seq, t.role, t.content) for t in ctx.recent])

    await svc.ltm.remember(org_id=org.id, scope="user", user_id=ravi.id, content="Ravi is vegetarian",
                           kind="preference")
    await svc.ltm.remember(org_id=org.id, scope="org", content="Acme orders team lunch on Fridays",
                           created_by=admin.id)
    print("long-term recall:", [(m.content, round(m.score, 2)) for m in
                                await svc.ltm.recall(org_id=org.id, user_id=ravi.id, query="what does ravi eat")])

    scope = LLMCacheScope(org.id, ravi.id, "claude-haiku-4-5-20251001", "intent", eff.version)
    print("\nllm cache before:", await svc.llm_cache.lookup(scope, "Repeat my last Swiggy order"))
    await svc.llm_cache.store(scope, "Repeat my last Swiggy order", {"site": "swiggy.com", "goal": "reorder_last"})
    print("exact hit:", await svc.llm_cache.lookup(scope, "Repeat my last Swiggy order"))
    print("near-wording hit:", await svc.llm_cache.lookup(scope, "repeat my last swiggy order please"))

    keys = sorted(k.decode() for k in await svc.redis.keys(f"{settings.redis_prefix}:*"))
    print(f"\nredis keys ({len(keys)}):")
    for k in keys:
        print("  ", k[:100])
    await svc.close()


if __name__ == "__main__":
    asyncio.run(main())
