"""Password hashing (Argon2id) and password policy."""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.errors import ErrorDetail, ValidationFailedError

MIN_LENGTH = 12
MAX_LENGTH = 128
_MIN_DISTINCT_CHARACTERS = 5

# argon2-cffi defaults follow RFC 9106 (Argon2id, 64 MiB, 3 passes, 4 lanes).
_hasher = PasswordHasher()

# Verified against when the account does not exist, so that the response time
# does not reveal whether an email is registered.
_DUMMY_HASH = _hasher.hash("timing-equalisation-placeholder")

# Passwords long enough to pass the length rule but far too common to allow.
_COMMON_PASSWORDS = frozenset(
    {
        "123456789012",
        "1234567890123",
        "12345678901234",
        "123456789abc",
        "1q2w3e4r5t6y",
        "1qaz2wsx3edc",
        "abcdefghijkl",
        "administrator",
        "changeme1234",
        "iloveyou1234",
        "letmein12345",
        "password1234",
        "password12345",
        "passwordpassword",
        "qwerty123456",
        "qwertyuiop12",
        "qwertyuiop123",
        "qwertyuiopas",
        "welcome12345",
        "zaq12wsxcde3",
    }
)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Constant-shape verification: always performs one Argon2 computation."""
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def validate_password_policy(password: str, *, email: str, full_name: str = "") -> None:
    """Raise ValidationFailedError if the password is too weak.

    Length is the main requirement; there are no composition rules.
    """
    problems: list[str] = []
    lowered = password.lower()

    if len(password) < MIN_LENGTH:
        problems.append(f"must be at least {MIN_LENGTH} characters long")
    if len(password) > MAX_LENGTH:
        problems.append(f"must be at most {MAX_LENGTH} characters long")
    if len(set(password)) < _MIN_DISTINCT_CHARACTERS:
        problems.append("must not be a repeated character or very short pattern")
    if lowered in _COMMON_PASSWORDS:
        problems.append("is too common")

    local_part = email.split("@", 1)[0].lower()
    personal = [local_part, email.lower(), *(part.lower() for part in full_name.split())]
    if any(len(value) >= 4 and value in lowered for value in personal):
        problems.append("must not contain your name or email address")

    if problems:
        raise ValidationFailedError(
            "The password does not meet the password policy.",
            code="weak_password",
            errors=[
                ErrorDetail(field="password", code="weak_password", message=message)
                for message in problems
            ],
        )
