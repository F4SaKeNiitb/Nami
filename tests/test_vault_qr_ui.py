"""SecureInput's QR upload decodes on the device and the secret goes only through the vault API."""

from pathlib import Path

SRC = (Path(__file__).parent.parent / "public/elements/SecureInput.jsx").read_text()


def test_qr_is_decoded_locally_with_a_graceful_fallback():
    assert "BarcodeDetector" in SRC and "Paste the setup key instead" in SRC


def test_the_qr_image_is_never_sent_anywhere():
    body = SRC[SRC.index("async function readQr"):SRC.index("export default function")]
    assert "fetch(" not in body and "FormData" not in body and "callAction" not in body


def test_secrets_never_use_callaction():
    assert "callAction(" not in SRC and "sendUserMessage" not in SRC
