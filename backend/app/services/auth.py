"""Registration, login, sessions and password management."""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import BadRequestError, ConflictError, UnauthenticatedError
from app.core.rate_limit import RateLimiter, too_many
from app.core.redis import RedisClient
from app.core.security.passwords import (
    hash_password,
    needs_rehash,
    validate_password_policy,
    verify_password,
)
from app.core.security.tokens import create_access_token, generate_opaque_token, hash_token
from app.db.context import set_current_user
from app.db.errors import is_unique_violation
from app.models import PasswordResetToken, RefreshToken, User
from app.repositories.identity import (
    PasswordResetRepository,
    RefreshTokenRepository,
    UserRepository,
)
from app.schemas.auth import LoginRequest, RegisterRequest
from app.services.audit import AuditEvent, AuditService, RequestMeta
from app.services.email import EmailSender, password_reset_email
from app.services.organizations import OrganizationService
from app.utils.ids import uuid7

logger = logging.getLogger(__name__)

_REVOKED_SESSION_PREFIX = "auth:revoked_session:"
_INVALID_CREDENTIALS = "Invalid email or password."


@dataclass(frozen=True, slots=True)
class IssuedSession:
    user: User
    access_token: str
    refresh_token: str
    refresh_token_id: uuid.UUID
    session_id: uuid.UUID


def revoked_session_key(session_id: uuid.UUID) -> str:
    return f"{_REVOKED_SESSION_PREFIX}{session_id}"


class AuthService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        redis: RedisClient,
        settings: Settings,
        audit: AuditService,
        email: EmailSender,
        meta: RequestMeta,
    ) -> None:
        self._session = session
        self._redis = redis
        self._settings = settings
        self._audit = audit
        self._email = email
        self._meta = meta
        self._limiter = RateLimiter(redis)
        self._users = UserRepository(session)
        self._refresh_tokens = RefreshTokenRepository(session)
        self._reset_tokens = PasswordResetRepository(session)

    # --- registration and login ------------------------------------------------

    async def register(self, data: RegisterRequest) -> IssuedSession:
        await self._limiter.enforce(
            "register:ip",
            self._meta.ip or "unknown",
            limit=self._settings.register_ip_limit,
            window_s=self._settings.register_ip_window_s,
        )
        validate_password_policy(data.password, email=data.email, full_name=data.full_name)

        if await self._users.get_by_email(data.email) is not None:
            raise ConflictError("An account with this email already exists.", code="email_taken")

        user = User(
            id=uuid7(),
            email=data.email,
            full_name=data.full_name,
            password_hash=hash_password(data.password),
            password_changed_at=datetime.now(UTC),
        )
        try:
            await self._users.add(user)
        except IntegrityError as exc:
            if is_unique_violation(exc):  # lost a race with a concurrent registration
                raise ConflictError(
                    "An account with this email already exists.", code="email_taken"
                ) from exc
            raise
        await set_current_user(self._session, user.id)

        organizations = OrganizationService(
            session=self._session,
            settings=self._settings,
            audit=self._audit,
            email=self._email,
        )
        if data.invite_token is not None:
            membership = await organizations.accept_invitation(user, data.invite_token)
            organization_id = membership.organization_id
        else:
            assert data.organization_name is not None  # noqa: S101 - guaranteed by the schema
            organization = await organizations.create(user, data.organization_name)
            organization_id = organization.id

        await self._audit.record(
            self._session,
            AuditEvent(
                action="user.register",
                actor_id=user.id,
                organization_id=organization_id,
                resource_type="user",
                resource_id=user.id,
            ),
        )
        return await self._issue_session(user)

    async def login(self, data: LoginRequest) -> IssuedSession:
        settings = self._settings
        await self._limiter.enforce(
            "login:ip",
            self._meta.ip or "unknown",
            limit=settings.login_ip_limit,
            window_s=settings.login_ip_window_s,
        )
        failures = await self._limiter.peek(
            "login:fail", data.email, limit=settings.login_max_failures
        )
        if failures.count >= settings.login_max_failures:
            raise too_many(
                failures,
                code="account_locked",
                message="Too many failed sign-in attempts. Please try again later.",
            )

        user = await self._users.get_by_email(data.email)
        password_ok = verify_password(data.password, user.password_hash if user else None)
        if user is None or not password_ok or not user.is_active:
            await self._limiter.hit(
                "login:fail",
                data.email,
                limit=settings.login_max_failures,
                window_s=settings.login_failure_window_s,
            )
            await self._audit.record_detached(
                AuditEvent(
                    action="auth.login_failed",
                    actor_type="system",
                    resource_type="user",
                    resource_id=user.id if user else None,
                    changes={"reason": "inactive" if user and password_ok else "bad_credentials"},
                )
            )
            # One message for every cause, so the response does not reveal which emails exist.
            raise UnauthenticatedError(_INVALID_CREDENTIALS, code="invalid_credentials")

        await self._limiter.reset("login:fail", data.email)
        if needs_rehash(user.password_hash):
            user.password_hash = hash_password(data.password)
        user.last_login_at = datetime.now(UTC)
        await self._audit.record(
            self._session,
            AuditEvent(
                action="auth.login", actor_id=user.id, resource_type="user", resource_id=user.id
            ),
        )
        return await self._issue_session(user)

    # --- sessions ----------------------------------------------------------------

    async def refresh(self, raw_refresh_token: str | None) -> IssuedSession:
        """Exchange a refresh token for a new pair. Each refresh token works once."""
        invalid = UnauthenticatedError(
            "The session has expired. Please sign in again.", code="invalid_refresh_token"
        )
        if not raw_refresh_token:
            raise invalid
        stored = await self._refresh_tokens.get_by_hash_for_update(hash_token(raw_refresh_token))
        if stored is None:
            raise invalid

        now = datetime.now(UTC)
        if stored.revoked_at is not None:
            if stored.replaced_by is not None:
                # A token that was already exchanged is being presented again: either it
                # was stolen or the legitimate client is replaying. End the whole session.
                await self._revoke_session(stored.family_id, now)
                await self._session.commit()  # must survive the 401 raised below
                await self._audit.record_detached(
                    AuditEvent(
                        action="auth.refresh_token_reuse",
                        actor_type="system",
                        resource_type="user",
                        resource_id=stored.user_id,
                        changes={"session_id": str(stored.family_id)},
                    )
                )
                logger.warning("refresh_token_reuse", extra={"user_id": str(stored.user_id)})
            raise invalid
        if stored.expires_at <= now:
            raise invalid

        user = await self._users.get(stored.user_id)
        if user is None or not user.is_active:
            raise invalid

        replacement = await self._issue_session(user, family_id=stored.family_id)
        stored.revoked_at = now
        stored.replaced_by = replacement.refresh_token_id
        return replacement

    async def logout(self, raw_refresh_token: str | None) -> None:
        """End the session the refresh token belongs to. Unknown tokens are ignored."""
        if not raw_refresh_token:
            return
        stored = await self._refresh_tokens.get_by_hash_for_update(hash_token(raw_refresh_token))
        if stored is None:
            return
        await self._revoke_session(stored.family_id, datetime.now(UTC))
        await self._audit.record(
            self._session,
            AuditEvent(
                action="auth.logout",
                actor_id=stored.user_id,
                resource_type="user",
                resource_id=stored.user_id,
            ),
        )

    async def logout_everywhere(self, user: User) -> None:
        await self._revoke_all_sessions(user.id)
        await self._audit.record(
            self._session,
            AuditEvent(
                action="auth.logout_all",
                actor_id=user.id,
                resource_type="user",
                resource_id=user.id,
            ),
        )

    # --- passwords ---------------------------------------------------------------

    async def change_password(
        self, user: User, *, current_password: str, new_password: str
    ) -> IssuedSession:
        if not verify_password(current_password, user.password_hash):
            raise BadRequestError("The current password is incorrect.", code="wrong_password")
        await self._set_password(user, new_password)
        await self._audit.record(
            self._session,
            AuditEvent(
                action="auth.password_changed",
                actor_id=user.id,
                resource_type="user",
                resource_id=user.id,
            ),
        )
        # Every existing session is ended; the caller continues on a fresh one.
        return await self._issue_session(user)

    async def request_password_reset(self, email: str) -> None:
        """Always succeeds from the caller's point of view."""
        settings = self._settings
        window_s = settings.password_reset_window_s
        # One address may serve several people, so the per-IP allowance is larger.
        await self._limiter.enforce(
            "reset:ip",
            self._meta.ip or "unknown",
            limit=settings.password_reset_limit * 5,
            window_s=window_s,
        )
        await self._limiter.enforce(
            "reset:email", email, limit=settings.password_reset_limit, window_s=window_s
        )

        user = await self._users.get_by_email(email)
        if user is None or not user.is_active:
            return
        now = datetime.now(UTC)
        await self._reset_tokens.invalidate_for_user(user.id, now)
        token = generate_opaque_token()
        await self._reset_tokens.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=hash_token(token),
                expires_at=now + timedelta(seconds=settings.password_reset_ttl_s),
            )
        )
        await self._audit.record(
            self._session,
            AuditEvent(
                action="auth.password_reset_requested",
                actor_type="system",
                resource_type="user",
                resource_id=user.id,
            ),
        )
        await self._email.send(
            password_reset_email(
                to=user.email,
                base_url=settings.public_base_url,
                token=token,
                ttl_minutes=settings.password_reset_ttl_s // 60,
            )
        )

    async def reset_password(self, *, token: str, new_password: str) -> None:
        invalid = BadRequestError(
            "The reset link is invalid or has expired.", code="invalid_reset_token"
        )
        stored = await self._reset_tokens.get_by_hash_for_update(hash_token(token))
        now = datetime.now(UTC)
        if stored is None or stored.used_at is not None or stored.expires_at <= now:
            raise invalid
        user = await self._users.get(stored.user_id)
        if user is None or not user.is_active:
            raise invalid

        await self._set_password(user, new_password)
        stored.used_at = now
        await self._limiter.reset("login:fail", user.email)
        await self._audit.record(
            self._session,
            AuditEvent(
                action="auth.password_reset",
                actor_id=user.id,
                resource_type="user",
                resource_id=user.id,
            ),
        )

    # --- helpers -----------------------------------------------------------------

    async def _set_password(self, user: User, new_password: str) -> None:
        validate_password_policy(new_password, email=user.email, full_name=user.full_name)
        user.password_hash = hash_password(new_password)
        user.password_changed_at = datetime.now(UTC)
        await self._revoke_all_sessions(user.id)

    async def _issue_session(self, user: User, family_id: uuid.UUID | None = None) -> IssuedSession:
        settings = self._settings
        # From here on the transaction acts as this user (row-level security).
        await set_current_user(self._session, user.id)
        session_id = family_id or uuid7()
        refresh_token = generate_opaque_token()
        stored = await self._refresh_tokens.add(
            RefreshToken(
                user_id=user.id,
                family_id=session_id,
                token_hash=hash_token(refresh_token),
                expires_at=datetime.now(UTC) + timedelta(seconds=settings.refresh_token_ttl_s),
                user_agent=(self._meta.user_agent or "")[:512] or None,
                ip=self._meta.ip,
            )
        )
        access_token = create_access_token(
            user_id=user.id,
            session_id=session_id,
            secret=settings.jwt_secret_key.get_secret_value(),
            ttl_s=settings.access_token_ttl_s,
            issuer=settings.app_name,
        )
        return IssuedSession(
            user=user,
            access_token=access_token,
            refresh_token=refresh_token,
            refresh_token_id=stored.id,
            session_id=session_id,
        )

    async def _revoke_session(self, session_id: uuid.UUID, now: datetime) -> None:
        await self._refresh_tokens.revoke_family(session_id, now)
        await self._deny_access_tokens([session_id])

    async def _revoke_all_sessions(self, user_id: uuid.UUID) -> None:
        session_ids = await self._refresh_tokens.revoke_all_for_user(user_id, datetime.now(UTC))
        await self._deny_access_tokens(session_ids)

    async def _deny_access_tokens(self, session_ids: Sequence[uuid.UUID]) -> None:
        """Access tokens of a revoked session stop working at once, not when they expire."""
        if not session_ids:
            return
        async with self._redis.pipeline(transaction=False) as pipe:
            for session_id in session_ids:
                pipe.set(
                    revoked_session_key(session_id), "1", ex=self._settings.access_token_ttl_s + 60
                )
            await pipe.execute()
