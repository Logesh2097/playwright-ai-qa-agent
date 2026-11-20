import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.errors import UnauthenticatedError, ValidationFailedError
from app.core.security.passwords import (
    hash_password,
    needs_rehash,
    validate_password_policy,
    verify_password,
)
from app.core.security.permissions import (
    ROLE_PERMISSIONS,
    Permission,
    Role,
    can_manage_role,
    has_permission,
)
from app.core.security.tokens import (
    create_access_token,
    decode_access_token,
    generate_opaque_token,
    hash_token,
)
from app.db.errors import is_unique_violation, to_app_error
from app.utils.ids import uuid7
from app.utils.redaction import REDACTED, redact

SECRET = "unit-test-signing-key-0123456789abcdef"
ISSUER = "qa-agent"


# --- passwords -------------------------------------------------------------------


def test_password_is_hashed_with_argon2id_and_verifies() -> None:
    hashed = hash_password("correct horse battery staple")
    assert hashed.startswith("$argon2id$")
    assert "correct horse" not in hashed
    assert verify_password("correct horse battery staple", hashed)
    assert not verify_password("wrong password entirely", hashed)
    assert not needs_rehash(hashed)


def test_same_password_hashes_differently_each_time() -> None:
    assert hash_password("correct horse battery staple") != hash_password(
        "correct horse battery staple"
    )


def test_verify_without_a_hash_is_false_and_does_not_raise() -> None:
    assert not verify_password("anything at all", None)
    assert not verify_password("anything at all", "not-a-valid-hash")


@pytest.mark.parametrize(
    ("password", "reason"),
    [
        ("short", "at least 12"),
        ("a" * 129, "at most 128"),
        ("aaaaaaaaaaaaaaaa", "repeated"),
        ("password1234", "too common"),
        ("asha.rao-secret-1", "name or email"),
        ("my name is Asha!!", "name or email"),
    ],
)
def test_weak_passwords_are_rejected_with_a_reason(password: str, reason: str) -> None:
    with pytest.raises(ValidationFailedError) as excinfo:
        validate_password_policy(password, email="asha.rao@acme.io", full_name="Asha Rao")
    assert excinfo.value.code == "weak_password"
    assert any(reason in error.message for error in excinfo.value.errors)
    assert all(error.field == "password" for error in excinfo.value.errors)


def test_long_passphrase_without_special_characters_is_accepted() -> None:
    validate_password_policy("correct horse battery staple", email="asha@acme.io")


# --- access tokens ---------------------------------------------------------------


def _token(**overrides: object) -> str:
    arguments: dict[str, object] = {
        "user_id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "secret": SECRET,
        "ttl_s": 900,
        "issuer": ISSUER,
    }
    arguments.update(overrides)
    return create_access_token(**arguments)  # type: ignore[arg-type]


def test_access_token_round_trip() -> None:
    user_id, session_id = uuid.uuid4(), uuid.uuid4()
    claims = decode_access_token(
        _token(user_id=user_id, session_id=session_id), secret=SECRET, issuer=ISSUER
    )
    assert claims.user_id == user_id
    assert claims.session_id == session_id
    assert claims.expires_at > datetime.now(UTC)


def test_expired_access_token_is_rejected_as_expired() -> None:
    token = _token(now=datetime.now(UTC) - timedelta(hours=1))
    with pytest.raises(UnauthenticatedError) as excinfo:
        decode_access_token(token, secret=SECRET, issuer=ISSUER)
    assert excinfo.value.code == "token_expired"


@pytest.mark.parametrize(
    "token",
    [
        "not-a-jwt",
        "",
        _token(secret="a-different-signing-key-0123456789abcdef"),
        _token(issuer="someone-else"),
        # "alg: none" forgery
        "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJzdWIiOiIxIn0.",
    ],
)
def test_invalid_access_tokens_are_rejected(token: str) -> None:
    with pytest.raises(UnauthenticatedError) as excinfo:
        decode_access_token(token, secret=SECRET, issuer=ISSUER)
    assert excinfo.value.code == "invalid_token"


def test_opaque_tokens_are_unique_and_stored_only_as_hashes() -> None:
    first, second = generate_opaque_token(), generate_opaque_token()
    assert first != second
    assert len(first) >= 43
    assert hash_token(first) == hash_token(first)
    assert first not in hash_token(first)
    assert len(hash_token(first)) == 64


# --- permissions -----------------------------------------------------------------


def test_every_role_has_a_permission_set() -> None:
    assert set(ROLE_PERMISSIONS) == set(Role)


@pytest.mark.parametrize(
    ("role", "permission", "allowed"),
    [
        (Role.VIEWER, Permission.ORG_READ, True),
        (Role.VIEWER, Permission.MEMBERS_READ, True),
        (Role.VIEWER, Permission.ORG_UPDATE, False),
        (Role.DEVELOPER, Permission.MEMBERS_MANAGE, False),
        (Role.QA_ENGINEER, Permission.AUDIT_READ, False),
        (Role.ADMIN, Permission.MEMBERS_MANAGE, True),
        (Role.ADMIN, Permission.AUDIT_READ, True),
        (Role.ADMIN, Permission.ORG_DELETE, False),
        (Role.OWNER, Permission.ORG_DELETE, True),
    ],
)
def test_permission_matrix(role: Role, permission: Permission, allowed: bool) -> None:
    assert has_permission(role, permission) is allowed


def test_owner_has_every_permission() -> None:
    assert ROLE_PERMISSIONS[Role.OWNER] == frozenset(Permission)


def test_roles_cannot_manage_roles_above_their_own() -> None:
    assert can_manage_role(Role.ADMIN, Role.ADMIN)
    assert can_manage_role(Role.ADMIN, Role.VIEWER)
    assert not can_manage_role(Role.ADMIN, Role.OWNER)
    assert can_manage_role(Role.OWNER, Role.OWNER)
    assert not can_manage_role(Role.DEVELOPER, Role.QA_ENGINEER)


# --- redaction, ids, database errors ---------------------------------------------


def test_redact_masks_sensitive_keys_at_any_depth() -> None:
    data = {
        "email": "a@b.io",
        "password": "hunter2",
        "nested": {"refresh_token": "abc", "items": [{"api_key": "k", "name": "ok"}]},
        "Authorization": "Bearer x",
    }
    assert redact(data) == {
        "email": "a@b.io",
        "password": REDACTED,
        "nested": {"refresh_token": REDACTED, "items": [{"api_key": REDACTED, "name": "ok"}]},
        "Authorization": REDACTED,
    }
    assert data["password"] == "hunter2", "the input must not be modified"


def test_uuid7_is_version_7_and_time_ordered() -> None:
    values = [uuid7() for _ in range(50)]
    assert all(value.version == 7 for value in values)
    assert len(set(values)) == 50
    first = uuid7()
    later = uuid.UUID(int=first.int + (5 << 80))  # 5 ms later
    assert first < later


class _FakeDriverError(Exception):
    def __init__(self, sqlstate: str, constraint_name: str | None = None) -> None:
        super().__init__("duplicate key value violates unique constraint (email)=(x@y.io)")
        self.sqlstate = sqlstate
        self.constraint_name = constraint_name


def test_database_errors_map_to_safe_responses() -> None:
    unique = IntegrityError("INSERT", {}, _FakeDriverError("23505", "uq_users_email"))
    assert is_unique_violation(unique)
    assert is_unique_violation(unique, "uq_users_email")
    assert not is_unique_violation(unique, "another_constraint")

    mapped = to_app_error(unique)
    assert mapped.status_code == 409
    assert "x@y.io" not in (mapped.detail or "")

    foreign_key = to_app_error(IntegrityError("INSERT", {}, _FakeDriverError("23503")))
    assert (foreign_key.status_code, foreign_key.code) == (409, "reference_conflict")

    check = to_app_error(IntegrityError("INSERT", {}, _FakeDriverError("23514")))
    assert check.status_code == 422

    lost = OperationalError("SELECT", {}, _FakeDriverError("08006"), connection_invalidated=True)
    assert (to_app_error(lost).status_code, to_app_error(lost).code) == (
        503,
        "database_unavailable",
    )

    forbidden = to_app_error(OperationalError("SELECT", {}, _FakeDriverError("42501")))
    assert forbidden.status_code == 500
    assert forbidden.detail is None
