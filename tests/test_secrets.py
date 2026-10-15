from app.security.secrets import contains_secret, redact


def test_detects_common_secrets():
    for s in ["sk-or-v1-0123456789abcdef0123456789", "password: hunter2", "JBSWY3DPEHPK3PXP",
              "JBSW Y3DP EHPK 3PXP", "otpauth://totp/X?secret=ABC", "4111-1111-1111-1111",
              "AKIAABCDEFGHIJKLMNOP"]:
        assert contains_secret(s), s


def test_ignores_ordinary_text():
    for s in ["Repeat my last Swiggy order", "Order 1234567890 shipped", "INTERNATIONALIZATION",
              "call me at 9876543210"]:
        assert not contains_secret(s), s


def test_redact():
    assert redact("key sk-or-v1-0123456789abcdef0123456789 ok") == "key [REDACTED:api_key] ok"
