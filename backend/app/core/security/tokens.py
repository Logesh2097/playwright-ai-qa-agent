"""Access tokens (JWT) and opaque single-purpose tokens."""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from app.core.errors import UnauthenticatedError

_ALGORITHM = "HS256"
_TOKEN_TYPE = "access"  # noqa: S105 - a claim value, not a secret


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    user_id: uuid.UUID
    session_id: uuid.UUID
    expires_at: datetime


def create_access_token(
    *,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    secret: str,
    ttl_s: int,
    issuer: str,
    now: datetime | None = None,
) -> str:
    """Short-lived bearer token.

    It carries identity and session only. Roles are read from the database on
    every request, so a role change or removal takes effect immediately.
    """
    issued_at = now or datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "sid": str(session_id),
        "typ": _TOKEN_TYPE,
        "iss": issuer,
        "iat": issued_at,
        "exp": issued_at + timedelta(seconds=ttl_s),
        "jti": secrets.token_hex(8),
    }
    return jwt.encode(payload, secret, algorithm=_ALGORITHM)


def decode_access_token(token: str, *, secret: str, issuer: str) -> AccessTokenClaims:
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[_ALGORITHM],
            issuer=issuer,
            options={"require": ["exp", "iat", "sub", "sid", "iss"]},
        )
        if payload.get("typ") != _TOKEN_TYPE:
            raise jwt.InvalidTokenError("wrong token type")
        return AccessTokenClaims(
            user_id=uuid.UUID(payload["sub"]),
            session_id=uuid.UUID(payload["sid"]),
            expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        )
    except jwt.ExpiredSignatureError as exc:
        raise UnauthenticatedError("The access token has expired.", code="token_expired") from exc
    except (jwt.InvalidTokenError, ValueError, KeyError) as exc:
        raise UnauthenticatedError("The access token is invalid.", code="invalid_token") from exc


def generate_opaque_token() -> str:
    """Random URL-safe token (256 bits) for refresh, reset and invitation links."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Only this hash is stored. The token is high-entropy, so a fast hash is appropriate."""
    return hashlib.sha256(token.encode()).hexdigest()
