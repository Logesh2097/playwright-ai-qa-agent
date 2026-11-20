import base64
import os
import uuid

import pytest

from app.core.security.crypto import (
    CredentialCipher,
    DecryptionError,
    EncryptedSecret,
    parse_master_keys,
)

ORG, CRED = uuid.uuid4(), uuid.uuid4()
SECRET = {"username": "cust01@acme.io", "password": "S3cr3t-Passw0rd!"}


def key() -> bytes:
    return os.urandom(32)


def test_round_trip() -> None:
    cipher = CredentialCipher({1: key()})
    encrypted = cipher.encrypt(SECRET, organization_id=ORG, credential_id=CRED)
    assert cipher.decrypt(encrypted, organization_id=ORG, credential_id=CRED) == SECRET
    assert encrypted.key_version == 1


def test_ciphertext_does_not_contain_the_secret_and_differs_every_time() -> None:
    cipher = CredentialCipher({1: key()})
    first = cipher.encrypt(SECRET, organization_id=ORG, credential_id=CRED)
    second = cipher.encrypt(SECRET, organization_id=ORG, credential_id=CRED)
    for value in SECRET.values():
        assert value.encode() not in first.ciphertext
        assert value.encode() not in first.wrapped_dek
    assert first.ciphertext != second.ciphertext
    assert first.wrapped_dek != second.wrapped_dek
    assert "S3cr3t" not in repr(first)


def test_ciphertext_is_bound_to_its_organization_and_credential() -> None:
    """A row copied to another tenant or credential must not decrypt."""
    cipher = CredentialCipher({1: key()})
    encrypted = cipher.encrypt(SECRET, organization_id=ORG, credential_id=CRED)
    with pytest.raises(DecryptionError):
        cipher.decrypt(encrypted, organization_id=uuid.uuid4(), credential_id=CRED)
    with pytest.raises(DecryptionError):
        cipher.decrypt(encrypted, organization_id=ORG, credential_id=uuid.uuid4())


def test_tampering_is_detected() -> None:
    cipher = CredentialCipher({1: key()})
    encrypted = cipher.encrypt(SECRET, organization_id=ORG, credential_id=CRED)
    flipped = bytes([encrypted.ciphertext[0] ^ 1]) + encrypted.ciphertext[1:]
    tampered = EncryptedSecret(flipped, encrypted.nonce, encrypted.wrapped_dek, 1)
    with pytest.raises(DecryptionError):
        cipher.decrypt(tampered, organization_id=ORG, credential_id=CRED)


def test_wrong_or_missing_master_key_cannot_decrypt() -> None:
    encrypted = CredentialCipher({1: key()}).encrypt(
        SECRET, organization_id=ORG, credential_id=CRED
    )
    with pytest.raises(DecryptionError):
        CredentialCipher({1: key()}).decrypt(encrypted, organization_id=ORG, credential_id=CRED)
    with pytest.raises(DecryptionError):
        CredentialCipher({2: key()}).decrypt(encrypted, organization_id=ORG, credential_id=CRED)


def test_master_key_rotation() -> None:
    old, new = key(), key()
    before = CredentialCipher({1: old}).encrypt(SECRET, organization_id=ORG, credential_id=CRED)

    rotated = CredentialCipher({1: old, 2: new})
    assert rotated.active_version == 2
    # Old data stays readable; new data and re-wrapped data use the new key.
    assert rotated.decrypt(before, organization_id=ORG, credential_id=CRED) == SECRET
    rewrapped = rotated.rewrap(before, organization_id=ORG, credential_id=CRED)
    assert rewrapped.key_version == 2
    assert (
        CredentialCipher({2: new}).decrypt(rewrapped, organization_id=ORG, credential_id=CRED)
        == SECRET
    )


def test_parse_master_keys() -> None:
    first, second = key(), key()
    raw = f"1:{base64.b64encode(first).decode()}, 2:{base64.b64encode(second).decode()}"
    assert parse_master_keys(raw) == {1: first, 2: second}


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "no-version",
        "x:AAAA",
        "1:not base64",
        "1:" + base64.b64encode(b"too short").decode(),
        "0:" + base64.b64encode(b"k" * 32).decode(),
        f"1:{base64.b64encode(b'k' * 32).decode()},1:{base64.b64encode(b'j' * 32).decode()}",
    ],
)
def test_invalid_master_key_settings_are_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_master_keys(raw)
