"""AES-256-GCM with associated data.

The AAD binds a ciphertext to its owner, id, kind and allowed origins. Moving a row to another
user, retyping its kind, or widening its origin list in the database makes decryption fail,
so those rules are enforced by cryptography and not only by application code.
"""

import json
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class DecryptionError(Exception):
    """Wrong key, tampered ciphertext or tampered AAD. Deliberately gives no detail."""


def new_dek() -> bytes:
    return AESGCM.generate_key(bit_length=256)


def build_aad(*, user_id: str, credential_id: str, kind: str, origins: list[str]) -> bytes:
    return "|".join([user_id, credential_id, kind, ",".join(sorted(origins))]).encode()


@dataclass(frozen=True)
class Sealed:
    ciphertext: bytes
    nonce: bytes


def seal(dek: bytes, payload: dict[str, str], aad: bytes) -> Sealed:
    nonce = os.urandom(12)
    data = json.dumps(payload, separators=(",", ":")).encode()
    return Sealed(AESGCM(dek).encrypt(nonce, data, aad), nonce)


def open_sealed(dek: bytes, sealed: Sealed, aad: bytes) -> dict[str, str]:
    try:
        return json.loads(AESGCM(dek).decrypt(sealed.nonce, sealed.ciphertext, aad))
    except (InvalidTag, ValueError):
        raise DecryptionError("cannot decrypt") from None
