"""Learned-workflows API: auth, CSRF, ownership, hash-pinned approval, freeze."""

from types import SimpleNamespace

import httpx
import pytest
from chainlit.auth import get_current_user
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI, Request

import app.core.runtime as rt
from app.skills.router import router
from app.skills.schemas import SkillContent
from app.db.models import Skill
from app.skills.service import SkillError, SkillService
from app.skills.signing import Signer

H = {"X-Requested-With": "nami"}


@pytest.fixture
async def w(svc, monkeypatch):
    admin = await svc.tenancy.ensure_user(google_sub="g1", email="a@x.com", name="A")
    mem = await svc.tenancy.ensure_user(google_sub="g2", email="m@x.com", name="M")
    org = await svc.tenancy.create_org(name="Acme", owner_id=admin.id)
    await svc.tenancy.add_member(org_id=org.id, user_id=mem.id, role="member", actor_id=admin.id)
    skills = SkillService(svc.db, Signer(ec.generate_private_key(ec.SECP256R1())))
    monkeypatch.setattr(rt, "_runtime", SimpleNamespace(orchestrator=SimpleNamespace(skills=skills)))
    app = FastAPI()
    app.include_router(router)

    async def who(request: Request):
        u = request.headers.get("x-test-user")
        return SimpleNamespace(metadata={"user_id": u, "org_id": org.id}) if u else None
    app.dependency_overrides[get_current_user] = who
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

    async def propose(user, code=None):
        c_ = SkillContent(site="acme.com", intent="reorder last order", steps=["Open Orders"], code=code)
        return await skills.propose(org_id=org.id, user_id=user.id, content=c_, observed_domains=["acme.com"], plan_domains=["acme.com"])
    yield SimpleNamespace(c=c, admin=admin, mem=mem, skills=skills, propose=propose, org_id=org.id)
    await c.aclose()


def as_(u, **k):
    return {"x-test-user": u.id, **k}


async def test_requires_sign_in_and_csrf(w):
    sk = await w.propose(w.mem)
    assert (await w.c.get("/chainlit/skills")).status_code == 401
    assert (await w.c.delete(f"/chainlit/skills/{sk.id}", headers=as_(w.mem))).status_code == 403
    assert (await w.c.post(f"/chainlit/skills/{sk.id}/approve", json={}, headers=as_(w.mem))).status_code == 403


async def test_list_shows_own_skills_hash_and_scan_but_not_other_users(w):
    mine, theirs = await w.propose(w.mem), await w.propose(w.admin)
    rows = (await w.c.get("/chainlit/skills", headers=as_(w.mem))).json()
    assert [r["id"] for r in rows] == [mine.id] and rows[0]["sha256"] == mine.content_sha256 and rows[0]["scan"]["passed"]
    assert theirs.id not in str(rows)


async def test_other_users_skill_looks_missing_for_approve_and_revoke(w):
    sk = await w.propose(w.admin, code="document.title")        # a candidate snippet owned by admin
    r = await w.c.post(f"/chainlit/skills/{sk.id}/approve", json={"expected_sha256": sk.content_sha256}, headers=as_(w.mem, **H))
    assert r.status_code == 404
    assert (await w.c.delete(f"/chainlit/skills/{sk.id}", headers=as_(w.mem, **H))).status_code == 404
    assert (await w.skills.get(sk.id)).status == "candidate"


async def test_snippet_approval_needs_the_hash_the_user_saw(w):
    sk = await w.propose(w.mem, code="document.title")
    bad = await w.c.post(f"/chainlit/skills/{sk.id}/approve", json={"expected_sha256": "0" * 64}, headers=as_(w.mem, **H))
    assert bad.status_code == 404 and (await w.skills.get(sk.id)).status == "candidate"
    ok = await w.c.post(f"/chainlit/skills/{sk.id}/approve", json={"expected_sha256": sk.content_sha256}, headers=as_(w.mem, **H))
    assert ok.status_code == 200 and ok.json()["status"] == "approved"


async def test_revoke_then_gone_from_the_list_and_freeze_returns_423(w):
    sk = await w.propose(w.mem)
    assert (await w.c.delete(f"/chainlit/skills/{sk.id}", headers=as_(w.mem, **H))).status_code == 200
    assert (await w.c.get("/chainlit/skills", headers=as_(w.mem))).json() == []
    cand = await w.propose(w.mem, code="document.title")
    w.skills.freeze = True
    r = await w.c.post(f"/chainlit/skills/{cand.id}/approve", json={"expected_sha256": cand.content_sha256}, headers=as_(w.mem, **H))
    assert r.status_code == 423


# ───────────── snippet sandbox (real Chromium) ─────────────

import http.server
import threading

from app.browser.local_provider import LocalPlaywrightProvider
from app.browser.provider import browser_session
from app.skills.sandbox import run_snippet


class _Page(http.server.BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self):
        _Page.hits.append(self.path)
        body = (b"<html><title>Orders</title><body><h1>hello</h1>"
                b"<button onclick=\"fetch('http://localhost:%d/stolen')\">go</button></body></html>" % self.server.other_port)
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a): ...


def _serve(other_port=1):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Page)
    srv.other_port = other_port
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
async def browser(tmp_path):
    p = LocalPlaywrightProvider(tmp_path / "profiles")
    try:
        s = await p.open("probe"); await p.close(s)
    except Exception as e:
        pytest.skip(f"chromium unavailable: {e}")
    yield p
    await p.shutdown()


@pytest.fixture
async def snip(w, browser):
    """An approved snippet for 127.0.0.1 with a page open on that server."""
    other = _serve(); srv = _serve(other.server_address[1]); _Page.hits.clear()
    site = "127.0.0.1"

    async def make(code, *, approve=True):
        c = SkillContent(site=site, intent="read the heading", steps=["Run the snippet"], code=code)
        sk = await w.skills.propose(org_id=w.org_id, user_id=w.mem.id,
                                    content=c, observed_domains=[site], plan_domains=[site])
        if approve and sk.status == "candidate":
            sk = await w.skills.approve(sk.id, org_id=w.org_id, actor_id=w.mem.id, expected_sha256=sk.content_sha256)
        return sk
    async with browser_session(browser, "u1") as s:
        from app.browser.cdp import default_context
        async with default_context(s.cdp_url) as ctx:
            page = await ctx.new_page()
            await page.goto(f"http://127.0.0.1:{srv.server_address[1]}/")
        yield SimpleNamespace(s=s, make=make, plan=[site], other=other, w=w)
    srv.shutdown(); other.shutdown()


async def _run(snip, sk, **kw):
    return await run_snippet(snip.w.skills, sk.id, user_id=snip.w.mem.id, org_id=snip.w.org_id, cdp_url=snip.s.cdp_url,
                             plan_domains=kw.pop("plan", snip.plan), **kw)


@pytest.mark.browser
async def test_approved_snippet_runs_in_the_page_and_returns_a_capped_result(snip):
    sk = await snip.make("return document.querySelector('h1').textContent")
    r = await _run(snip, sk)
    assert r.ok and r.output == "hello"
    big = await snip.make("return 'x'.repeat(100000)")
    assert len((await _run(snip, big)).output) <= 2000


@pytest.mark.browser
async def test_unapproved_tampered_or_out_of_plan_snippet_does_not_run(snip):
    cand = await snip.make("return 1", approve=False)
    with pytest.raises(SkillError):
        await _run(snip, cand)
    ok = await snip.make("return 2")
    with pytest.raises(SkillError):
        await _run(snip, ok, plan=["other.com"])
    from sqlalchemy import update
    async with snip.w.skills.db.session() as s:
        await s.execute(update(Skill).where(Skill.id == ok.id).values(content={**ok.content, "code": "return 3"}))
    with pytest.raises(SkillError):
        await _run(snip, ok)


@pytest.mark.browser
async def test_runaway_snippet_is_killed_by_the_timeout(snip):
    sk = await snip.make("while(true){}")
    r = await _run(snip, sk, timeout_ms=500)
    assert not r.ok and r.output == ""


@pytest.mark.browser
async def test_requests_to_sites_outside_the_plan_are_blocked_during_a_run(snip):
    sk = await snip.make("document.querySelector('button').click(); return 'clicked'")
    r = await _run(snip, sk)
    assert r.ok and r.output == "clicked"
    assert r.blocked_requests >= 1 and "/stolen" not in _Page.hits
