"""Key-encryption keys. The KEK never leaves the provider; we only ask it to wrap and unwrap DEKs."""

import os
from abc import ABC, abstractmethod

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class KmsError(Exception):
    pass


class KmsProvider(ABC):
    kek_id: str

    @abstractmethod
    def wrap(self, dek: bytes) -> bytes: ...

    @abstractmethod
    def unwrap(self, wrapped: bytes) -> bytes: ...


class LocalKms(KmsProvider):
    """Dev and test KEK held in process memory. Production uses Azure Key Vault (RSA-OAEP-256)."""

    def __init__(self, kek: bytes | None = None, kek_id: str = "local-dev"):
        if kek is not None and len(kek) != 32:
            raise ValueError("KEK must be 32 bytes")
        self._kek = kek or AESGCM.generate_key(bit_length=256)
        self.kek_id = kek_id

    def wrap(self, dek: bytes) -> bytes:
        nonce = os.urandom(12)
        return nonce + AESGCM(self._kek).encrypt(nonce, dek, self.kek_id.encode())

    def unwrap(self, wrapped: bytes) -> bytes:
        try:
            return AESGCM(self._kek).decrypt(wrapped[:12], wrapped[12:], self.kek_id.encode())
        except (InvalidTag, ValueError):
            raise KmsError("cannot unwrap key") from None


class AzureKeyVaultKms(KmsProvider):
    """Wraps per-user DEKs with an RSA key that never leaves Azure Key Vault (RSA-OAEP-256).

    Standard-tier Key Vault supports RSA wrap/unwrap; AES key-wrap needs Managed HSM, so it is not used.
    """

    def __init__(self, vault_url: str, key_name: str):
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.keys import KeyClient
        from azure.keyvault.keys.crypto import CryptographyClient

        key = KeyClient(vault_url, DefaultAzureCredential()).get_key(key_name)
        self.kek_id = key.id
        self._crypto = CryptographyClient(key, DefaultAzureCredential())

    def wrap(self, dek: bytes) -> bytes:
        from azure.keyvault.keys.crypto import KeyWrapAlgorithm
        return self._crypto.wrap_key(KeyWrapAlgorithm.rsa_oaep_256, dek).encrypted_key

    def unwrap(self, wrapped: bytes) -> bytes:
        from azure.keyvault.keys.crypto import KeyWrapAlgorithm
        try:
            return self._crypto.unwrap_key(KeyWrapAlgorithm.rsa_oaep_256, wrapped).key
        except Exception:
            raise KmsError("cannot unwrap key") from None
