"""Envelope encryption for stored credentials.

Each credential gets its own random data key (DEK), which encrypts the secret
payload with AES-256-GCM. The DEK is itself encrypted ("wrapped") with a master
key (KEK) that only exists in the service's environment. Both operations bind
the ciphertext to the organization and credential IDs, so a row copied into
another tenant or onto another credential cannot be decrypted.

Rotating the master key means adding a higher-numbered key: new and re-saved
credentials use it, old ones stay readable until they are re-wrapped.
"""

import base64
import binascii
import json
import os
import uuid
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_KEY_BYTES = 32
_NONCE_BYTES = 12


class DecryptionError(Exception):
    """The ciphertext could not be authenticated with the available keys and context."""


@dataclass(frozen=True, slots=True)
class EncryptedSecret:
    ciphertext: bytes
    nonce: bytes
    wrapped_dek: bytes
    key_version: int

    def __repr__(self) -> str:  # never let key material end up in a log line
        return f"EncryptedSecret(key_version={self.key_version})"


def parse_master_keys(raw: str) -> dict[int, bytes]:
    """Parse ``"1:<base64 key>,2:<base64 key>"`` into ``{version: key}``."""
    keys: dict[int, bytes] = {}
    for entry in filter(None, (part.strip() for part in raw.split(","))):
        version_text, separator, encoded = entry.partition(":")
        if not separator or not version_text.isdigit():
            raise ValueError("each master key must look like '<version>:<base64 key>'")
        try:
            key = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("master key is not valid base64") from exc
        if len(key) != _KEY_BYTES:
            raise ValueError("master key must be 32 bytes (256 bits) before encoding")
        version = int(version_text)
        if version < 1 or version in keys:
            raise ValueError("master key versions must be unique positive integers")
        keys[version] = key
    if not keys:
        raise ValueError("at least one master key is required")
    return keys


def _aad(organization_id: uuid.UUID, credential_id: uuid.UUID) -> bytes:
    return f"credential:{organization_id}:{credential_id}".encode()


class CredentialCipher:
    def __init__(self, keys: dict[int, bytes]) -> None:
        if not keys:
            raise ValueError("at least one master key is required")
        self._keys = dict(keys)
        self.active_version = max(keys)

    def encrypt(
        self, payload: dict[str, str], *, organization_id: uuid.UUID, credential_id: uuid.UUID
    ) -> EncryptedSecret:
        aad = _aad(organization_id, credential_id)
        dek = AESGCM.generate_key(bit_length=256)
        nonce = os.urandom(_NONCE_BYTES)
        plaintext = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        ciphertext = AESGCM(dek).encrypt(nonce, plaintext, aad)

        wrap_nonce = os.urandom(_NONCE_BYTES)
        wrapped = AESGCM(self._keys[self.active_version]).encrypt(wrap_nonce, dek, aad)
        return EncryptedSecret(
            ciphertext=ciphertext,
            nonce=nonce,
            wrapped_dek=wrap_nonce + wrapped,
            key_version=self.active_version,
        )

    def decrypt(
        self, secret: EncryptedSecret, *, organization_id: uuid.UUID, credential_id: uuid.UUID
    ) -> dict[str, str]:
        kek = self._keys.get(secret.key_version)
        if kek is None:
            raise DecryptionError(f"master key version {secret.key_version} is not available")
        aad = _aad(organization_id, credential_id)
        try:
            dek = AESGCM(kek).decrypt(
                secret.wrapped_dek[:_NONCE_BYTES], secret.wrapped_dek[_NONCE_BYTES:], aad
            )
            plaintext = AESGCM(dek).decrypt(secret.nonce, secret.ciphertext, aad)
        except InvalidTag as exc:
            raise DecryptionError("credential could not be decrypted") from exc
        payload: dict[str, str] = json.loads(plaintext)
        return payload

    def rewrap(
        self, secret: EncryptedSecret, *, organization_id: uuid.UUID, credential_id: uuid.UUID
    ) -> EncryptedSecret:
        """Re-encrypt under the active master key (used when rotating master keys)."""
        return self.encrypt(
            self.decrypt(secret, organization_id=organization_id, credential_id=credential_id),
            organization_id=organization_id,
            credential_id=credential_id,
        )
