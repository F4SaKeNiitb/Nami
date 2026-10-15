"""Vault service: no leaks, origin binding, isolation, revocation, shredding, tamper detection."""

import glob

import pyotp
import pytest
from sqlalchemy import text

from app.vault import totp
from app.vault.kms import LocalKms
from app.vault.service import (KeyDestroyed, NotFound, OriginDenied, SecretPayload, VaultError,
                               VaultService)

PW = "Tr0ub4dor&3-unique-marker"
APIKEY = "sk-or-v1-abcdef0123456789abcdef0123456789"
SEED = "JBSWY3DPEHPK3PXP"
CARD = "4111111111111111"


@pytest.fixture
async def vault(svc):
    return VaultService(svc.db, LocalKms())


@pytest.fixture
async def alice(svc):
    return (await svc.tenancy.ensure_user(google_sub="g-a", email="a@x.com", name="A")).id


@pytest.fixture
async def bob(svc):
    return (await svc.tenancy.ensure_user(google_sub="g-b", email="b@x.com", name="B")).id


async def store_pw(vault, user, origins=("swiggy.com",)):
    return await vault.store(user_id=user, kind="password", label="Swiggy login",
                             secret={"username": "me@x.com", "password": PW}, origins=list(origins))


# ───────────── no leaks ─────────────

async def test_secret_is_not_in_the_database_files(svc, vault, alice):
    await store_pw(vault, alice)
    await vault.store(user_id=alice, kind="api_key", label="OpenRouter", secret={"key": APIKEY})
    await vault.store(user_id=alice, kind="card", label="Visa", origins=["swiggy.com"],
                      secret={"number": CARD, "cvv": "123", "exp": "12/29"})
    await vault.store(user_id=alice, kind="totp", label="Swiggy 2FA", secret={"seed": SEED}, origins=["swiggy.com"])
    async with svc.db.engine.begin() as c:
        await c.execute(text("PRAGMA wal_checkpoint(FULL)"))
    blob = b"".join(open(f, "rb").read() for f in glob.glob(str(svc.db.engine.url.database) + "*"))
    for needle in (PW, APIKEY, CARD, SEED, "123/"):
        assert needle.encode() not in blob, f"plaintext {needle[:6]}… found on disk"


async def test_views_and_log_hold_no_secret_material(svc, vault, alice):
    v = await store_pw(vault, alice)
    await vault.use(user_id=alice, credential_id=v.id, origin="https://swiggy.com", actor="executor")
    dump = repr(await vault.list(alice)) + repr([(r.__dict__) for r in await vault.access_log(alice)])
    assert PW not in dump


async def test_decrypted_payload_does_not_print(vault, alice):
    v = await store_pw(vault, alice)
    p = await vault.use(user_id=alice, credential_id=v.id, origin="https://swiggy.com", actor="executor")
    assert isinstance(p, SecretPayload) and p["password"] == PW
    assert PW not in repr(p) and PW not in str(p) and PW not in f"{p}"


async def test_display_meta_is_derived_not_supplied(vault, alice):
    k = await vault.store(user_id=alice, kind="api_key", label="OR", secret={"key": APIKEY})
    c = await vault.store(user_id=alice, kind="card", label="Visa", origins=["x.com"],
                          secret={"number": "4111 1111 1111 1111", "cvv": "123", "exp": "12/29"})
    assert k.meta == {"last4": APIKEY[-4:]}
    assert c.meta == {"last4": "1111", "exp": "12/29"} and "123" not in str(c.meta)


async def test_label_that_is_a_secret_is_refused(vault, alice):
    with pytest.raises(VaultError):
        await vault.store(user_id=alice, kind="api_key", label=APIKEY, secret={"key": APIKEY})


# ───────────── origin binding ─────────────

@pytest.mark.parametrize("origin", [
    "https://swiggy.com.evil.com", "https://evil.com", "http://swiggy.com", "https://swlggy.com", "", "about:blank"])
async def test_use_on_wrong_origin_is_refused_and_logged(vault, alice, origin):
    v = await store_pw(vault, alice)
    with pytest.raises(OriginDenied):
        await vault.use(user_id=alice, credential_id=v.id, origin=origin, actor="executor", task_id="t1")
    last = (await vault.access_log(alice, v.id))[-1]
    assert (last.action, last.result, last.reason, last.task_id) == ("inject", "denied", "origin not allowed", "t1")


async def test_subdomain_is_allowed(vault, alice):
    v = await store_pw(vault, alice)
    p = await vault.use(user_id=alice, credential_id=v.id, origin="https://www.swiggy.com/login", actor="executor")
    assert p["username"] == "me@x.com"


async def test_password_card_totp_require_a_website(vault, alice):
    for kind, secret in [("password", {"username": "u", "password": "p"}),
                         ("card", {"number": CARD, "cvv": "1", "exp": "1/29"}), ("totp", {"seed": SEED})]:
        with pytest.raises(VaultError):
            await vault.store(user_id=alice, kind=kind, label="x", secret=secret)


# ───────────── isolation ─────────────

async def test_other_user_cannot_use_list_or_revoke(vault, alice, bob):
    v = await store_pw(vault, alice)
    with pytest.raises(NotFound):
        await vault.use(user_id=bob, credential_id=v.id, origin="https://swiggy.com", actor="executor")
    with pytest.raises(NotFound):
        await vault.revoke(user_id=bob, credential_id=v.id)
    assert await vault.list(bob) == []
    assert (await vault.list(alice))[0].id == v.id           # untouched
    assert [r.result for r in await vault.access_log(bob)] == ["denied", "denied"]
    assert await vault.access_log(alice, v.id) != [] and all(r.user_id == alice for r in await vault.access_log(alice))


async def test_missing_and_foreign_look_identical(vault, alice, bob):
    v = await store_pw(vault, alice)
    errs = []
    for cid in (v.id, "does-not-exist"):
        with pytest.raises(NotFound) as e:
            await vault.use(user_id=bob, credential_id=cid, origin="https://swiggy.com", actor="x")
        errs.append(str(e.value))
    assert errs[0] == errs[1]


async def test_cannot_link_another_users_credential(vault, alice, bob):
    v = await store_pw(vault, alice)
    with pytest.raises(NotFound):
        await vault.store(user_id=bob, kind="totp", label="2fa", secret={"seed": SEED},
                          origins=["swiggy.com"], linked_credential_id=v.id)


# ───────────── tamper detection ─────────────

async def test_widening_origin_list_in_db_breaks_decryption(svc, vault, alice):
    v = await store_pw(vault, alice)
    async with svc.db.engine.begin() as c:
        await c.execute(text("UPDATE credentials SET origin_allowlist = :o WHERE id = :i"),
                        {"o": '["swiggy.com", "evil.com"]', "i": v.id})
    with pytest.raises(VaultError):
        await vault.use(user_id=alice, credential_id=v.id, origin="https://evil.com", actor="executor")
    assert (await vault.access_log(alice, v.id))[-1].reason == "integrity check failed"


async def test_retyping_kind_or_moving_row_to_another_user_breaks_decryption(svc, vault, alice, bob):
    a = await store_pw(vault, alice)
    async with svc.db.engine.begin() as c:
        await c.execute(text("UPDATE credentials SET user_id = :b WHERE id = :i"), {"b": bob, "i": a.id})
    await vault.store(user_id=bob, kind="api_key", label="k", secret={"key": APIKEY})   # gives bob a key
    with pytest.raises(VaultError):
        await vault.use(user_id=bob, credential_id=a.id, origin="https://swiggy.com", actor="executor")


async def test_swapping_ciphertext_between_credentials_fails(svc, vault, alice):
    a, b = await store_pw(vault, alice), await store_pw(vault, alice, ("zomato.com",))
    async with svc.db.engine.begin() as c:
        row = (await c.execute(text("SELECT ciphertext, nonce FROM credentials WHERE id = :i"), {"i": b.id})).one()
        await c.execute(text("UPDATE credentials SET ciphertext = :c, nonce = :n WHERE id = :i"),
                        {"c": row[0], "n": row[1], "i": a.id})
    with pytest.raises(VaultError):
        await vault.use(user_id=alice, credential_id=a.id, origin="https://swiggy.com", actor="executor")


# ───────────── revoke / shred ─────────────

async def test_revoke_wipes_ciphertext_blocks_use_and_hides_from_list(svc, vault, alice):
    v = await store_pw(vault, alice)
    await vault.revoke(user_id=alice, credential_id=v.id)
    async with svc.db.engine.connect() as c:
        row = (await c.execute(text("SELECT length(ciphertext), length(nonce), revoked_at FROM credentials"))).one()
    assert row[0] == 0 and row[1] == 0 and row[2] is not None
    with pytest.raises(NotFound):
        await vault.use(user_id=alice, credential_id=v.id, origin="https://swiggy.com", actor="executor")
    assert await vault.list(alice) == [] and len(await vault.list(alice, include_revoked=True)) == 1
    acts = [(r.action, r.result) for r in await vault.access_log(alice, v.id)]
    assert ("revoke", "allowed") in acts and acts[-1] == ("inject", "denied")   # history survives


async def test_revoking_a_password_revokes_its_linked_totp(vault, alice):
    pw = await store_pw(vault, alice)
    t = await vault.store(user_id=alice, kind="totp", label="2fa", secret={"seed": SEED},
                          origins=["swiggy.com"], linked_credential_id=pw.id)
    await vault.revoke(user_id=alice, credential_id=pw.id)
    with pytest.raises(NotFound):
        await vault.totp_code(user_id=alice, credential_id=t.id, origin="https://swiggy.com", actor="x")


async def test_shredding_the_user_key_makes_everything_unreadable_forever(svc, vault, alice, bob):
    a = await store_pw(vault, alice)
    b = await store_pw(vault, bob)
    await vault.shred_user(alice)
    with pytest.raises(NotFound):
        await vault.use(user_id=alice, credential_id=a.id, origin="https://swiggy.com", actor="x")
    with pytest.raises(KeyDestroyed):                       # key cannot be silently recreated
        await store_pw(vault, alice)
    assert (await vault.use(user_id=bob, credential_id=b.id, origin="https://swiggy.com", actor="x"))["password"] == PW


async def test_destroyed_key_row_alone_blocks_decryption(svc, vault, alice):
    """Even if ciphertext were restored from a backup, the wrapped key is gone."""
    v = await store_pw(vault, alice)
    async with svc.db.engine.connect() as c:
        saved = (await c.execute(text("SELECT ciphertext, nonce FROM credentials"))).one()
    await vault.shred_user(alice)
    async with svc.db.engine.begin() as c:
        await c.execute(text("UPDATE credentials SET ciphertext=:c, nonce=:n, revoked_at=NULL"), {"c": saved[0], "n": saved[1]})
    with pytest.raises(KeyDestroyed):
        await vault.use(user_id=alice, credential_id=v.id, origin="https://swiggy.com", actor="x")


async def test_wrong_kek_cannot_open_the_vault(svc, alice):
    first = VaultService(svc.db, LocalKms())
    v = await store_pw(first, alice)
    other = VaultService(svc.db, LocalKms())                # a different KEK
    with pytest.raises(Exception):
        await other.use(user_id=alice, credential_id=v.id, origin="https://swiggy.com", actor="x")


# ───────────── validation / TOTP ─────────────

@pytest.mark.parametrize("kind,secret", [
    ("password", {"password": "p"}), ("password", {"username": ""}),      # a username is required; the password is optional
    ("api_key", {}), ("card", {"number": CARD}), ("nope", {"key": "x"})])
async def test_incomplete_or_unknown_secrets_refused(vault, alice, kind, secret):
    with pytest.raises(VaultError):
        await vault.store(user_id=alice, kind=kind, label="x", secret=secret, origins=["x.com"])


async def test_invalid_totp_key_refused_and_otpauth_uri_accepted(vault, alice):
    with pytest.raises(Exception):
        await vault.store(user_id=alice, kind="totp", label="2fa", secret={"seed": "nope!"}, origins=["x.com"])
    uri = pyotp.TOTP(SEED, digits=8).provisioning_uri(name="a", issuer_name="X")
    t = await vault.store(user_id=alice, kind="totp", label="2fa", secret={"seed": uri}, origins=["x.com"])
    assert t.meta == {"digits": 8, "period": 30, "algorithm": "SHA1"}


async def test_totp_code_matches_reference_and_is_origin_bound(vault, alice):
    t = await vault.store(user_id=alice, kind="totp", label="2fa", secret={"seed": SEED}, origins=["swiggy.com"])
    code, wait = await vault.totp_code(user_id=alice, credential_id=t.id, origin="https://swiggy.com", actor="x")
    assert code in {pyotp.TOTP(SEED).now(), pyotp.TOTP(SEED).at(__import__("time").time() + wait)}
    assert wait < 30
    with pytest.raises(OriginDenied):
        await vault.totp_code(user_id=alice, credential_id=t.id, origin="https://evil.com", actor="x")


async def test_use_updates_last_used_and_logs_actor_and_task(vault, alice):
    v = await store_pw(vault, alice)
    assert (await vault.list(alice))[0].last_used_at is None
    await vault.use(user_id=alice, credential_id=v.id, origin="https://swiggy.com", actor="agent:executor", task_id="t9")
    assert (await vault.list(alice))[0].last_used_at is not None
    last = (await vault.access_log(alice, v.id))[-1]
    assert (last.actor, last.task_id, last.result, last.origin) == ("agent:executor", "t9", "allowed", "https://swiggy.com")


async def test_login_without_a_password_is_stored_for_phone_and_code_sites(vault, alice):
    v = await vault.store(user_id=alice, kind="password", label="Swiggy", secret={"username": "6291445696", "password": ""},
                          origins=["swiggy.com"])
    data = await vault.use(user_id=alice, credential_id=v.id, origin="https://swiggy.com", actor="x")
    assert dict(data) == {"username": "6291445696"}               # the empty password was dropped, not stored as ""
