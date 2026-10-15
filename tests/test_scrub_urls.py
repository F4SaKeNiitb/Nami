"""A long id inside a URL is not a card number, but real cards (in prose or in a card-named parameter) are still caught."""

from app.security.secrets import redact

LUHN16 = "4111111111111111"        # passes the Luhn check


def test_an_order_id_in_a_url_parameter_is_kept():
    u = f"https://shop.example.com/track?order={LUHN16}&x=1"
    assert redact(u) == u


def test_an_id_in_a_url_path_is_kept():
    u = f"https://shop.example.com/orders/{LUHN16}/status"
    assert redact(u) == u


def test_a_card_typed_in_prose_is_still_redacted():
    assert redact(f"my card is {LUHN16} thanks") == "my card is [REDACTED:card_number] thanks"
    assert "[REDACTED:card_number]" in redact("pay with 4111 1111 1111 1111 now")


def test_a_card_named_url_parameter_is_still_redacted():
    out = redact(f"https://x.com/pay?card_number={LUHN16}")
    assert LUHN16 not in out and "[REDACTED:card_number]" in out


def test_a_grouped_card_inside_a_url_is_still_redacted():
    assert "[REDACTED:card_number]" in redact("https://x.com/?q=4111 1111 1111 1111")
