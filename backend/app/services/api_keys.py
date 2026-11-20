"""API keys: how a CI system proves who it is.

A key is shown once, when it is created. Only its SHA-256 hash is stored; the
key is long and random, so a fast hash is appropriate. A key belongs to one
organization, can be limited to one project, carries only the scopes it was
given, can expire and can be revoked.

A key is not a person: it cannot approve anything, manage anything or read
anything outside its scopes.
"""

import hashlib
import hmac
import re
import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ForbiddenError, NotFoundError, UnauthenticatedError
from app.core.security.permissions import Permission
from app.db.context import set_current_org
from app.models import ApiKey
from app.repositories.projects import ProjectRepository
from app.services.audit import AuditEvent, AuditService
from app.services.organizations import OrgContext

SCOPE_RUN = "executions:write"
SCOPE_READ = "executions:read"
SCOPES = (SCOPE_RUN, SCOPE_READ)
PREFIX = "qak"
# qak_<organization, 32 hex>_<secret>
_FORMAT = re.compile(r"^qak_([0-9a-f]{32})_([A-Za-z0-9_-]{40,64})$")
MAX_LIFETIME_DAYS = 366


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def generate_key(organization_id: uuid.UUID) -> str:
    return f"{PREFIX}_{organization_id.hex}_{secrets.token_urlsafe(32)}"


@dataclass(frozen=True, slots=True)
class ApiKeyContext:
    """A verified key and what it may do."""

    key: ApiKey

    @property
    def organization_id(self) -> uuid.UUID:
        return self.key.organization_id

    def require(self, scope: str) -> None:
        if scope not in self.key.scopes:
            raise ForbiddenError(
                f"This API key does not have the scope {scope}.", code="insufficient_scope"
            )

    def require_project(self, project_id: uuid.UUID) -> None:
        # Not found, not forbidden: a key learns nothing about projects it is not for.
        if self.key.project_id is not None and self.key.project_id != project_id:
            raise NotFoundError("Project not found.")


async def authenticate(session: AsyncSession, presented: str | None) -> ApiKeyContext:
    """The key's context, or 401. The same answer whatever is wrong with the key."""
    refused = UnauthenticatedError(
        "A valid API key is required.",
        code="invalid_api_key",
        headers={"WWW-Authenticate": "Bearer"},
    )
    match = _FORMAT.match(presented or "")
    if match is None:
        raise refused
    organization_id = uuid.UUID(hex=match.group(1))
    await set_current_org(session, organization_id)
    result = await session.execute(
        select(ApiKey).where(
            ApiKey.organization_id == organization_id, ApiKey.key_hash == hash_key(presented or "")
        )
    )
    key = result.scalar_one_or_none()
    now = datetime.now(UTC)
    if (
        key is None
        or not hmac.compare_digest(key.key_hash, hash_key(presented or ""))
        or key.revoked_at is not None
        or (key.expires_at is not None and key.expires_at <= now)
    ):
        raise refused
    # At most once a minute, so that polling does not write on every request.
    if key.last_used_at is None or now - key.last_used_at > timedelta(minutes=1):
        key.last_used_at = now
    return ApiKeyContext(key)


class ApiKeyService:
    def __init__(self, *, session: AsyncSession, audit: AuditService) -> None:
        self._session = session
        self._audit = audit

    async def list(self, org: OrgContext) -> Sequence[ApiKey]:
        org.require(Permission.INTEGRATIONS_MANAGE)
        result = await self._session.execute(
            select(ApiKey)
            .where(ApiKey.organization_id == org.organization.id)
            .order_by(ApiKey.created_at.desc())
        )
        return result.scalars().all()

    async def create(
        self,
        org: OrgContext,
        *,
        name: str,
        scopes: Sequence[str],
        project_id: uuid.UUID | None,
        expires_in_days: int | None,
    ) -> tuple[ApiKey, str]:
        """The stored key and, this once, the key itself."""
        org.require(Permission.INTEGRATIONS_MANAGE)
        if project_id is not None:
            project = await ProjectRepository(self._session, org.organization.id).get(project_id)
            if project is None:
                raise NotFoundError("Project not found.")
        secret = generate_key(org.organization.id)
        key = ApiKey(
            organization_id=org.organization.id,
            project_id=project_id,
            name=name,
            key_prefix=secret[: len(PREFIX) + 1 + 32 + 1 + 6],
            key_hash=hash_key(secret),
            scopes=sorted(set(scopes)),
            created_by=org.user.id,
            expires_at=(
                datetime.now(UTC) + timedelta(days=expires_in_days) if expires_in_days else None
            ),
        )
        self._session.add(key)
        await self._session.flush()
        await self._record(org, "api_key.create", key, {"name": name, "scopes": key.scopes})
        return key, secret

    async def revoke(self, org: OrgContext, key_id: uuid.UUID) -> ApiKey:
        org.require(Permission.INTEGRATIONS_MANAGE)
        key = await self._session.get(ApiKey, key_id)
        if key is None or key.organization_id != org.organization.id:
            raise NotFoundError("API key not found.")
        if key.revoked_at is None:
            key.revoked_at = datetime.now(UTC)
            await self._record(org, "api_key.revoke", key, {"name": key.name})
        return key

    async def _record(
        self, org: OrgContext, action: str, key: ApiKey, changes: dict[str, object]
    ) -> None:
        await self._audit.record(
            self._session,
            AuditEvent(
                action=action,
                actor_id=org.user.id,
                organization_id=org.organization.id,
                resource_type="api_key",
                resource_id=key.id,
                changes=dict(changes),
            ),
        )
