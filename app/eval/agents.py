"""Scripted stand-ins for browser-use, used to test the harness and the orchestrator around it (no model needed).

  brittle   hard-coded ids and labels: what a fragile automation script does. Drift should break it.
  adaptive  role/text based, opens menus and dismisses dialogs: roughly what a competent agent does.
Both type secrets only through placeholders from the broker payload, exactly like browser-use does.
"""

import re


from app.agent.schemas import Subgoal
from app.browser import cdp
from app.browser.executor import ExecResult

Q = {"login": re.compile(r"sign in|log in|login|enter", re.I), "reorder": re.compile(r"reorder|order again|again", re.I),
     "place": re.compile(r"place order|complete purchase|pay|finish", re.I)}


def _values(payload) -> dict[str, str]:
    flat = {}
    for bucket in (payload.sensitive_data or {}).values():
        flat.update(bucket)
    return flat


def _pick(values: dict[str, str], suffix: str) -> str | None:
    return next((v for k, v in values.items() if k.endswith(suffix)), None)


class FoodAgent:
    def __init__(self, kind: str = "adaptive", base: str = ""):
        assert kind in ("brittle", "adaptive")
        self.kind, self.base = kind, base
        self.ran: list[str] = []
        self.card_seen_on: list[str] = []                       # subgoals during which card placeholders were available

    async def run(self, *, session, subgoal: Subgoal, payload, **kw) -> ExecResult:
        self.ran.append(subgoal.goal)
        if any("card_" in p for p in payload.placeholders):
            self.card_seen_on.append(subgoal.goal)
        page = None
        try:
            async with cdp.connect(session.cdp_url) as browser:
                ctx = browser.contexts[0]
                page = next((p for p in reversed(ctx.pages) if p.url.startswith("http")), None) or await ctx.new_page()
                page.set_default_timeout(4000)
                g = subgoal.goal.lower()
                if subgoal.is_auth or g.startswith("sign in"):
                    await self._sign_in(page, payload)
                else:
                    # the planner may merge several reversible steps into one goal ("A. Then B. Then C."), so run every phase named
                    for part in g.split(" then "):
                        if "orders" in part:
                            await page.goto(self.base + "/orders")
                        elif "reorder" in part:
                            await self._reorder(page)
                        elif "checkout" in part or "review" in part:
                            await self._checkout(page)
                        elif "place" in part:
                            await self._place(page, payload)
                        elif "confirmation" in part:
                            await page.goto(self.base + "/confirmation")
                if "/login" in page.url or "/2fa" in page.url:
                    return ExecResult("failed", reason="the site asked me to sign in again", url=page.url, steps=1)
                return ExecResult("done", claim=f"did: {subgoal.goal}", url=page.url, steps=3)
        except Exception as e:
            if page is not None and ("/login" in page.url or "/2fa" in page.url):        # the real cause was a sign-in wall, not a missing button
                return ExecResult("failed", reason="the site asked me to sign in again", url=page.url, steps=1)
            return ExecResult("failed", reason=f"{type(e).__name__}: {str(e)[:120]}", steps=1)

    async def _dismiss(self, page):
        if self.kind == "adaptive":
            for sel in ("#close-modal", "text=No thanks"):
                if await page.locator(sel).count():
                    await page.locator(sel).first.click()

    async def _sign_in(self, page, payload):
        v = _values(payload)
        user, pw, code = _pick(v, "_username"), _pick(v, "_password"), _pick(v, "_2fa_code")
        await page.goto(self.base + "/orders")
        if "/login" not in page.url:
            return                                             # already signed in
        await self._dismiss(page)
        if self.kind == "brittle":
            await page.fill("#username", user); await page.fill("#password", pw); await page.click("#login")
        else:
            await page.locator("input:not([type=password])").first.fill(user)
            await page.locator("input[type=password]").fill(pw)
            await page.get_by_role("button", name=Q["login"]).first.click()
        await page.wait_for_load_state("domcontentloaded")
        if "/2fa" in page.url and code:
            await self._dismiss(page)
            # the vault computed this code; the seed never reaches the agent
            if self.kind == "brittle":
                await page.fill("#code", code); await page.click("#verify")
            else:
                await page.locator("input").first.fill(code)
                await page.get_by_role("button", name=re.compile("verify|submit", re.I)).first.click()
            await page.wait_for_load_state("domcontentloaded")

    async def _reorder(self, page):
        await page.goto(self.base + "/orders")
        await self._dismiss(page)
        if self.kind == "brittle":
            await page.click("#reorder-btn")
        else:
            if await page.get_by_text("⋯").count():
                await page.get_by_text("⋯").first.click()
            await page.get_by_text(Q["reorder"]).first.click()
        await page.wait_for_url("**/cart")

    async def _checkout(self, page):
        await page.goto(self.base + "/cart")
        await self._dismiss(page)
        await page.click("#to-checkout")
        await page.wait_for_load_state("domcontentloaded")
        await self._dismiss(page)
        if "/checkout" in page.url and await page.locator("#address").count():       # A/B variant: address step first
            if self.kind == "brittle":
                raise RuntimeError("unexpected extra step: address form")
            await page.get_by_role("button", name=re.compile("continue|next", re.I)).click()
            await page.wait_for_load_state("domcontentloaded")

    async def _place(self, page, payload):
        v = _values(payload)
        num, cvv = _pick(v, "_card_number"), _pick(v, "_card_cvv")
        if not num:
            raise RuntimeError("no card was released for this step")
        await self._dismiss(page)
        if self.kind == "brittle":
            await page.fill("#card", num); await page.fill("#cvv", cvv); await page.click("#place-order")
        else:
            inputs = page.locator("form input")
            await inputs.nth(0).fill(num); await inputs.nth(1).fill(cvv)
            if await page.get_by_text("⋯").count():
                await page.get_by_text("⋯").first.click()
            await page.get_by_role("button", name=Q["place"]).first.click()
        await page.wait_for_url("**/confirmation")
