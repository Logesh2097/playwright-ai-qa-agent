"""Getting at evidence: who may read a file, download addresses, and retention.

Evidence shows the application under test, sometimes with data in it. A file is
only ever served to someone who can see the project of the execution it
belongs to, and never from an address that works without that check.

A download address carries a signed token that names one file and one user and
is valid for a few minutes. Redeeming it repeats the whole check: the user must
still be active, still belong to the organization and still see the project,
and the file must still exist. It is a convenience for browsers (a link, a
video player), not a way to share a file.
"""

import logging
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import NotFoundError, UnauthenticatedError
from app.core.security.permissions import Permission
from app.db.context import set_current_org
from app.models import TestEvidence, TestExecution
from app.repositories.executions import EvidenceRepository
from app.services.audit import AuditEvent, AuditService
from app.services.evidence import EvidenceStorage, evidence_storage, extension_of
from app.services.projects import ProjectContext

logger = logging.getLogger(__name__)

_ALGORITHM = "HS256"
_TOKEN_TYPE = "evidence"  # noqa: S105 - a claim value, not a secret
NOT_FOUND = "Evidence not found. It may have expired."


@dataclass(frozen=True, slots=True)
class EvidenceFile:
    item: TestEvidence
    chunks: Iterator[bytes]

    @property
    def filename(self) -> str:
        return f"{self.item.kind}-{self.item.id}{extension_of(self.item.content_type)}"


@dataclass(frozen=True, slots=True)
class DownloadClaims:
    organization_id: uuid.UUID
    project_id: uuid.UUID
    execution_id: uuid.UUID
    evidence_id: uuid.UUID
    user_id: uuid.UUID
    # The sign-in session that asked. Signing out ends the address too.
    session_id: uuid.UUID


def sign_download(
    claims: DownloadClaims, *, secret: str, issuer: str, ttl_s: int
) -> tuple[str, datetime]:
    expires = datetime.now(UTC) + timedelta(seconds=ttl_s)
    token = jwt.encode(
        {
            "typ": _TOKEN_TYPE,
            "iss": issuer,
            "exp": expires,
            "sub": str(claims.user_id),
            "sid": str(claims.session_id),
            "org": str(claims.organization_id),
            "prj": str(claims.project_id),
            "exe": str(claims.execution_id),
            "evi": str(claims.evidence_id),
        },
        secret,
        algorithm=_ALGORITHM,
    )
    return token, expires


def verify_download(token: str, *, secret: str, issuer: str) -> DownloadClaims:
    """The claims of a valid, unexpired download token. Anything else is a 401."""
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[_ALGORITHM],
            issuer=issuer,
            options={"require": ["exp", "sub", "iss"]},
        )
        # An access token is signed with the same key; it must not work here, nor the reverse.
        if payload.get("typ") != _TOKEN_TYPE:
            raise jwt.InvalidTokenError("wrong token type")
        return DownloadClaims(
            organization_id=uuid.UUID(payload["org"]),
            project_id=uuid.UUID(payload["prj"]),
            execution_id=uuid.UUID(payload["exe"]),
            evidence_id=uuid.UUID(payload["evi"]),
            user_id=uuid.UUID(payload["sub"]),
            session_id=uuid.UUID(payload["sid"]),
        )
    except (jwt.InvalidTokenError, ValueError, KeyError) as exc:
        raise UnauthenticatedError(
            "The download address is invalid or has expired.", code="invalid_download_token"
        ) from exc


class EvidenceAccess:
    def __init__(self, *, session: AsyncSession, audit: AuditService, settings: Settings) -> None:
        self._session = session
        self._audit = audit
        self._settings = settings
        self._storage: EvidenceStorage = evidence_storage(settings)

    async def _item(
        self, ctx: ProjectContext, execution: TestExecution, evidence_id: uuid.UUID
    ) -> TestEvidence:
        ctx.require(Permission.PROJECTS_READ)
        item = await EvidenceRepository(self._session, ctx.organization_id).get_in_execution(
            execution.id, evidence_id
        )
        expired = (
            item is not None
            and item.expires_at is not None
            and item.expires_at <= datetime.now(UTC)
        )
        # A file whose redaction failed is kept away from everyone until it is deleted.
        if item is None or expired or item.redaction_status == "failed_quarantined":
            raise NotFoundError(NOT_FOUND)
        return item

    async def open(
        self, ctx: ProjectContext, execution: TestExecution, evidence_id: uuid.UUID
    ) -> EvidenceFile:
        item = await self._item(ctx, execution, evidence_id)
        chunks = self._storage.chunks(ctx.organization_id, item.storage_key)
        if chunks is None:
            raise NotFoundError(NOT_FOUND)
        return EvidenceFile(item, chunks)

    async def download_url(
        self,
        ctx: ProjectContext,
        execution: TestExecution,
        evidence_id: uuid.UUID,
        *,
        session_id: uuid.UUID,
    ) -> tuple[str, datetime]:
        """A token for one file, for this user, for a few minutes. Recorded in the audit log."""
        item = await self._item(ctx, execution, evidence_id)
        token, expires = sign_download(
            DownloadClaims(
                organization_id=ctx.organization_id,
                project_id=ctx.project.id,
                execution_id=execution.id,
                evidence_id=item.id,
                user_id=ctx.user.id,
                session_id=session_id,
            ),
            secret=self._settings.jwt_secret_key.get_secret_value(),
            issuer=self._settings.app_name,
            ttl_s=self._settings.evidence_url_ttl_s,
        )
        await self._audit.record(
            self._session,
            AuditEvent(
                action="evidence.download_url",
                actor_id=ctx.user.id,
                organization_id=ctx.organization_id,
                resource_type="test_evidence",
                resource_id=item.id,
                changes={"execution_id": str(execution.id), "kind": item.kind},
            ),
        )
        return token, expires


async def prune_expired(
    session_factory: async_sessionmaker[AsyncSession],
    storage: EvidenceStorage,
    *,
    now: datetime | None = None,
    batch: int = 500,
) -> dict[str, int]:
    """Delete evidence whose retention has ended: the file first, then its record.

    Goes organization by organization, inside each one's row-level security
    scope, so it never reads across tenants.
    """
    moment = now or datetime.now(UTC)
    removed = missing = 0
    for organization_id in storage.organizations():
        async with session_factory() as session, session.begin():
            await set_current_org(session, organization_id)
            rows: Any = await session.scalars(
                select(TestEvidence)
                .where(
                    TestEvidence.organization_id == organization_id,
                    TestEvidence.expires_at.is_not(None),
                    TestEvidence.expires_at <= moment,
                )
                .limit(batch)
            )
            for item in rows.all():
                if storage.delete(organization_id, item.storage_key):
                    removed += 1
                else:
                    missing += 1
                await session.delete(item)
    if removed or missing:
        logger.info("evidence_pruned", extra={"removed": removed, "already_missing": missing})
    return {"removed": removed, "already_missing": missing}
