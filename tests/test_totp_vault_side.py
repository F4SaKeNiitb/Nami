"""Every TOTP code (standard or not) is computed inside the vault; no seed reaches the broker payload or the agent."""

import json
import time

import pyotp
import pytest

from app.agent.schemas import AccessPlan, CredentialUse
from app.browser.broker import Broker
from app.vault.kms import LocalKms
from app.vault.service import VaultService

SEED = pyotp.random_base32(32)
PW = "Tr0ub4dor&3-unique-marker"


@pytest.fixture
async def vault(svc):
    return VaultService(svc.db, LocalKms())


@pytest.fixture
async def uid(svc):
    return (await svc.tenancy.ensure_user(google_sub="g-t", email="t@x.com", name="T")).id


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def no_wait(_):
        return None
    monkeypatch.setattr("app.browser.broker.asyncio.sleep", no_wait)


async def _access(vault, uid, seed):
    pw = await vault.store(user_id=uid, kind="password", label="s", origins=["swiggy.com"],
                           secret={"username": "me@x.com", "password": PW})
    tp = await vault.store(user_id=uid, kind="totp", label="s 2fa", origins=["swiggy.com"], secret={"seed": seed},
                           linked_credential_id=pw.id)
    cu = lambda v, k: CredentialUse(credential_id=v.id, kind=k, site="swiggy.com", label="x")
    return AccessPlan(allowed_domains=["swiggy.com"], login="password", credentials=[cu(pw, "password"), cu(tp, "totp")]), tp


async def test_standard_seed_is_never_in_the_payload(vault, uid):
    access, _ = await _access(vault, uid, SEED)
    p = await Broker(vault, user_id=uid, task_id="t1").build(access)
    assert SEED not in json.dumps(p.sensitive_data) and SEED not in repr(p)
    assert not any(n.endswith("bu_2fa_code") for n in p.placeholders)
    name = next(n for n in p.placeholders if n.endswith("_2fa_code"))
    code = p.sensitive_data["https://*.swiggy.com"][name]
    assert code.isdigit() and len(code) == 6


async def test_code_is_the_current_totp_for_the_seed(vault, uid):
    access, _ = await _access(vault, uid, SEED)
    p = await Broker(vault, user_id=uid, task_id="t1").build(access)
    name = next(n for n in p.placeholders if n.endswith("_2fa_code"))
    code = p.sensitive_data["https://*.swiggy.com"][name]
    totp = pyotp.TOTP(SEED)
    assert code in {totp.at(time.time() + d * 30) for d in (-1, 0, 1)}


async def test_the_code_use_is_logged_as_totp_and_the_seed_is_not_decrypted_for_injection(vault, uid):
    access, tp = await _access(vault, uid, SEED)
    await Broker(vault, user_id=uid, task_id="t1").build(access)
    rows = await vault.access_log(uid, tp.id)
    assert [r.action for r in rows if r.result == "allowed" and r.action not in ("store", "create")] == ["totp"]
