"""Run an approved snippet inside the remote browser, and nowhere else.

  * only a snippet that is approved, signature-valid, hash-matching and inside this task's plan can run;
  * the code is re-scanned just before it runs (network, eval, storage, cookies, navigation are refused);
  * it runs in an isolated JavaScript world of the current page, with a timeout and an output size cap;
  * every request the page makes during the run to a site outside the plan is aborted (egress policy).
There is no Node, shell or filesystem here: the code only ever sees one page's DOM.
"""

import asyncio
import json
from dataclasses import dataclass

from app.browser import cdp
from app.skills import scanner
from app.skills.schemas import Manifest, SkillContent
from app.skills.service import SkillError, SkillService
from app.vault.origins import registrable_domain

MAX_OUTPUT = 2000
TIMEOUT_MS = 3000


@dataclass
class SnippetResult:
    ok: bool
    output: str = ""
    error: str = ""
    blocked_requests: int = 0


async def run_snippet(skills: SkillService, skill_id: str, *, user_id: str, org_id: str, cdp_url: str,
                      plan_domains: list[str], task_id: str | None = None, timeout_ms: int = TIMEOUT_MS) -> SnippetResult:
    sk = await skills.get(skill_id, org_id)
    why = None
    if sk.kind != "snippet":
        why = "not a snippet"
    elif sk.org_id != org_id or (sk.user_id != user_id and sk.scope != "org"):
        why = "not available to this user"
    else:
        why = skills._verify(sk)
    if why:
        await skills._event(skill_id, "blocked", task_id, reason=why)
        raise SkillError(why)
    content = SkillContent.model_validate(sk.content)
    manifest = Manifest.model_validate(sk.manifest)
    allowed = sorted(set(manifest.domains) & set(plan_domains))
    if content.site not in allowed:
        await skills._event(skill_id, "blocked", task_id, reason="site outside the access plan")
        raise SkillError("site outside the access plan")
    if not scanner.scan(content, manifest).passed:               # re-scan at run time, not only at creation
        await skills._event(skill_id, "blocked", task_id, reason="rescan failed")
        raise SkillError("rescan failed")

    blocked = 0
    async with cdp.connect(cdp_url) as browser:
        ctx = browser.contexts[0]
        pages = [p for p in ctx.pages if p.url.startswith(("http://", "https://"))]
        page = pages[-1] if pages else None
        if page is None or registrable_domain(page.url) not in allowed:
            raise SkillError("the current page is outside the skill's domains")

        async def guard(route):
            nonlocal blocked
            if registrable_domain(route.request.url) in allowed or not route.request.url.startswith(("http://", "https://")):
                await route.continue_()
            else:
                blocked += 1
                await route.abort()
        await ctx.route("**/*", guard)
        try:
            session = await ctx.new_cdp_session(page)
            frame = (await session.send("Page.getFrameTree"))["frameTree"]["frame"]["id"]
            world = await session.send("Page.createIsolatedWorld", {"frameId": frame, "worldName": "nami-skill"})
            res = await session.send("Runtime.evaluate", {
                "expression": f"(function(){{ {content.code} }})()", "contextId": world["executionContextId"],
                "returnByValue": True, "timeout": timeout_ms, "awaitPromise": False})
        except Exception as e:                                    # timeout, protocol error
            await skills._event(skill_id, "blocked", task_id, reason="run failed", error=type(e).__name__)
            return SnippetResult(False, error=f"{type(e).__name__}: {str(e)[:120]}", blocked_requests=blocked)
        finally:
            await asyncio.sleep(0.2)                              # let requests the snippet started reach the guard
            await ctx.unroute("**/*", guard)
    if "exceptionDetails" in res:
        return SnippetResult(False, error=res["exceptionDetails"].get("text", "error")[:200], blocked_requests=blocked)
    value = res.get("result", {}).get("value")
    out = value if isinstance(value, str) else json.dumps(value)
    await skills._event(skill_id, "loaded", task_id, ran=True)
    return SnippetResult(True, output=(out or "")[:MAX_OUTPUT], blocked_requests=blocked)
