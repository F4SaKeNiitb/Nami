"""Hash and ES256-sign a skill so a stored row that was edited afterwards refuses to load."""

import hashlib
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def content_sha256(content: dict) -> str:
    return hashlib.sha256(canonical(content)).hexdigest()


def signed_fields(*, skill_id: str, org_id: str, user_id: str, scope: str, kind: str, site: str, intent: str,
                  version: int, sha256: str, manifest: dict) -> bytes:
    """Everything that decides who may use the skill and for what. Status is deliberately not signed: revocation
    is a database fact checked at load, so it cannot be undone by restoring an old signed row."""
    return canonical({"id": skill_id, "org": org_id, "user": user_id, "scope": scope, "kind": kind, "site": site,
                      "intent": intent, "version": version, "sha256": sha256, "manifest": manifest})


class Signer:
    def __init__(self, key: ec.EllipticCurvePrivateKey):
        self._key = key

    def sign(self, message: bytes) -> bytes:
        return self._key.sign(message, ec.ECDSA(hashes.SHA256()))

    def verify(self, message: bytes, signature: bytes | None) -> bool:
        if not signature:
            return False
        try:
            self._key.public_key().verify(signature, message, ec.ECDSA(hashes.SHA256()))
            return True
        except InvalidSignature:
            return False
