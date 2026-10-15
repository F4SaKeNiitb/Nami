"""Crypto layer: confidentiality, integrity, and binding of a secret to its owner and origins."""

import pytest

from app.vault.crypto import DecryptionError, build_aad, new_dek, open_sealed, seal
from app.vault.kms import KmsError, LocalKms

SECRET = {"password": "hunter2-Correct-Horse"}
AAD = build_aad(user_id="u1", credential_id="c1", kind="password", origins=["swiggy.com"])


def test_roundtrip():
    dek = new_dek()
    assert open_sealed(dek, seal(dek, SECRET, AAD), AAD) == SECRET


def test_ciphertext_does_not_contain_plaintext():
    dek = new_dek()
    assert b"hunter2" not in seal(dek, SECRET, AAD).ciphertext


def test_nonce_is_never_reused():
    dek = new_dek()
    assert len({seal(dek, SECRET, AAD).nonce for _ in range(200)}) == 200


@pytest.mark.parametrize("field,value", [
    ("user_id", "u2"), ("credential_id", "c2"), ("kind", "card"), ("origins", ["evil.com"]),
    ("origins", ["swiggy.com", "evil.com"]), ("origins", []),
])
def test_changing_any_bound_field_breaks_decryption(field, value):
    dek = new_dek()
    sealed = seal(dek, SECRET, AAD)
    args = dict(user_id="u1", credential_id="c1", kind="password", origins=["swiggy.com"])
    args[field] = value
    with pytest.raises(DecryptionError):
        open_sealed(dek, sealed, build_aad(**args))


def test_origin_order_does_not_matter():
    assert build_aad(user_id="u", credential_id="c", kind="k", origins=["b.com", "a.com"]) == \
           build_aad(user_id="u", credential_id="c", kind="k", origins=["a.com", "b.com"])


def test_flipped_ciphertext_bit_is_rejected():
    dek = new_dek()
    sealed = seal(dek, SECRET, AAD)
    bad = type(sealed)(bytes([sealed.ciphertext[0] ^ 1]) + sealed.ciphertext[1:], sealed.nonce)
    with pytest.raises(DecryptionError):
        open_sealed(dek, bad, AAD)


def test_another_users_key_cannot_decrypt():
    sealed = seal(new_dek(), SECRET, AAD)
    with pytest.raises(DecryptionError):
        open_sealed(new_dek(), sealed, AAD)


def test_decryption_error_leaks_nothing():
    with pytest.raises(DecryptionError) as e:
        open_sealed(new_dek(), seal(new_dek(), SECRET, AAD), AAD)
    assert "hunter2" not in str(e.value) and "u1" not in str(e.value)


def test_kms_wrap_roundtrip_and_wrong_kek_fails():
    a, b = LocalKms(), LocalKms()
    dek = new_dek()
    wrapped = a.wrap(dek)
    assert dek not in wrapped and a.unwrap(wrapped) == dek
    with pytest.raises(KmsError):
        b.unwrap(wrapped)


def test_kms_rejects_tampered_wrapped_key():
    kms = LocalKms()
    wrapped = bytearray(kms.wrap(new_dek()))
    wrapped[-1] ^= 1
    with pytest.raises(KmsError):
        kms.unwrap(bytes(wrapped))
