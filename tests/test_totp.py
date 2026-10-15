"""TOTP against pyotp as an independent reference, including window edges and parsing."""

import pyotp
import pytest

from app.vault import totp

SEED = "JBSWY3DPEHPK3PXP"


def test_matches_reference_over_many_timestamps():
    p = totp.parse(SEED)
    ref = pyotp.TOTP(SEED)
    for t in range(1_700_000_000, 1_700_000_000 + 3600, 97):
        assert totp.code_at(p, t) == ref.at(t)


def test_rfc6238_sha1_vector():
    # RFC 6238 appendix B: seed "12345678901234567890", T=59 -> 94287082 (8 digits)
    import base64
    seed = base64.b32encode(b"12345678901234567890").decode()
    assert totp.code_at(totp.TotpParams(seed, digits=8), 59) == "94287082"


def test_parse_variants():
    assert totp.parse("jbsw y3dp ehpk 3pxp").seed == SEED
    assert totp.parse("JBSW-Y3DP-EHPK-3PXP").seed == SEED


def test_parse_otpauth_uri_with_custom_params():
    uri = pyotp.TOTP(SEED, digits=8, interval=60).provisioning_uri(name="a@b.com", issuer_name="X")
    p = totp.parse(uri)
    assert (p.digits, p.period, p.algorithm) == (8, 60, "SHA1") and not p.standard
    assert totp.parse(SEED).standard


@pytest.mark.parametrize("bad", ["", "not base32 !!", "otpauth://hotp/x?secret=JBSWY3DPEHPK3PXP&counter=1", "1" * 20, "JBSWY3DP"])
def test_invalid_seeds_rejected(bad):
    with pytest.raises(totp.InvalidTotp):
        totp.parse(bad)


def test_verify_window_edges():
    p = totp.parse(SEED)
    t = 1_700_000_010
    prev, cur, nxt = totp.code_at(p, t - 30), totp.code_at(p, t), totp.code_at(p, t + 30)
    assert totp.verify(p, cur, t)
    assert totp.verify(p, prev, t) and totp.verify(p, nxt, t)         # one step of drift
    assert not totp.verify(p, totp.code_at(p, t + 90), t)             # three steps away
    assert not totp.verify(p, "000000" if cur != "000000" else "111111", t)


def test_fill_code_waits_for_next_window_when_about_to_expire():
    p = totp.parse(SEED)
    t = 1_700_000_000 - (1_700_000_000 % 30) + 27                      # 3 s left
    code, wait = totp.code_for_fill(p, at=t)
    assert wait > 2.9 and code == totp.code_at(p, t + 3)
    code, wait = totp.code_for_fill(p, at=t - 20)                      # 23 s left
    assert wait == 0 and code == totp.code_at(p, t - 20)


def test_repr_never_shows_the_seed():
    assert SEED not in repr(totp.parse(SEED))
