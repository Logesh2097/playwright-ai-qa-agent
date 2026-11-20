"""Audit trail of security-relevant actions."""

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_request_id
from app.models import AuditLog
from app.utils.redaction import redact

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RequestMeta:
    """Who is calling, as far as the network can tell."""

    ip: str | None = None
    user_agent: str | None = None


@dataclass(frozen=True, slots=True)
class AuditEvent:
    action: str
    actor_id: uuid.UUID | None = None
    actor_type: str = "user"
    organization_id: uuid.UUID | None = None
    resource_type: str | None = None
    resource_id: uuid.UUID | None = None
    changes: dict[str, Any] = field(default_factory=dict)


class AuditService:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], meta: RequestMeta | None = None
    ) -> None:
        self._session_factory = session_factory
        self._meta = meta or RequestMeta()

    def _row(self, event: AuditEvent) -> AuditLog:
        return AuditLog(
            organization_id=event.organization_id,
            actor_type=event.actor_type,
            actor_id=event.actor_id,
            action=event.action,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            request_id=get_request_id(),
            ip=self._meta.ip,
            user_agent=(self._meta.user_agent or "")[:512] or None,
            changes=redact(event.changes),
        )

    async def record(self, session: AsyncSession, event: AuditEvent) -> None:
        """Record inside the caller's transaction: the entry exists only if the action does."""
        session.add(self._row(event))
        await session.flush()

    async def record_detached(self, event: AuditEvent) -> None:
        """Record in a separate transaction.

        For events that must survive the rollback of the request that caused
        them, such as a failed login. A failure to write is logged, not raised:
        it must not turn a 401 into a 500.
        """
        try:
            async with self._session_factory() as session, session.begin():
                session.add(self._row(event))
        except Exception:
            logger.exception("audit_write_failed", extra={"action": event.action})
