"""What workflow nodes need from the outside world, in one place.

Nodes do not reach for globals. They are given this object, which tests
construct with fakes for the model, the engine and the browser.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.llm.base import LLMGateway
from app.browser.manager import BrowserSessionManager
from app.browser.services import EgressClient
from app.core.config import Settings
from app.core.redis import RedisClient
from app.core.security.crypto import CredentialCipher
from app.core.security.permissions import Permission, has_permission
from app.db.context import set_current_org, set_current_user
from app.engine.client import EngineClient
from app.models import User
from app.repositories.identity import MembershipRepository, OrganizationRepository
from app.repositories.projects import ProjectMemberRepository, ProjectRepository
from app.services.audit import AuditService
from app.services.organizations import OrgContext
from app.services.projects import ProjectContext


class WorkflowError(Exception):
    """A node could not do its work. The run ends as failed, with a report."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class CaptureQueue:
    """Collects jobs a service wants to enqueue, so a node can run them itself, in order."""

    pending: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def enqueue(self, job: str, **kwargs: Any) -> None:
        self.pending.append((job, kwargs))

    def take(self, job: str) -> dict[str, Any]:
        for index, (name, kwargs) in enumerate(self.pending):
            if name == job:
                del self.pending[index]
                return kwargs
        raise WorkflowError("internal_error", f"The service did not start the job {job}.")


@dataclass(frozen=True, slots=True)
class Acting:
    """A transaction in which a node acts as one particular person."""

    session: AsyncSession
    ctx: ProjectContext
    audit: AuditService


async def load_project_context(
    session: AsyncSession, organization_id: uuid.UUID, project_id: uuid.UUID, user_id: uuid.UUID
) -> ProjectContext:
    """The person's current position in the project. Checked every time, not remembered:
    someone removed from the organization since the run started can no longer act in it."""
    denied = WorkflowError(
        "actor_not_permitted", "The person this step acts for no longer has access to the project."
    )
    user = await session.get(User, user_id)
    membership = await MembershipRepository(session, organization_id).get_active_for_user(user_id)
    organization = await OrganizationRepository(session).get_active(organization_id)
    if user is None or membership is None or organization is None:
        raise denied
    org = OrgContext(user=user, organization=organization, membership=membership)
    project = await ProjectRepository(session, organization_id).get(project_id)
    if project is None:
        raise denied
    member = await ProjectMemberRepository(session, organization_id).get_for_user(
        project.id, user_id
    )
    if member is None and not has_permission(org.role, Permission.PROJECTS_ACCESS_ALL):
        raise denied
    return ProjectContext(org=org, project=project, membership=member)


@dataclass(slots=True)
class WorkflowDeps:
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings
    gateway: LLMGateway
    redis: RedisClient
    cipher: CredentialCipher
    engine: EngineClient | None = None
    egress: EgressClient | None = None
    browser_manager: BrowserSessionManager | None = None

    @asynccontextmanager
    async def acting(
        self, organization_id: str, project_id: str, user_id: str
    ) -> AsyncIterator[Acting]:
        """One transaction, as the given person, committed on leaving without an error."""
        organization, user = uuid.UUID(organization_id), uuid.UUID(user_id)
        async with self.session_factory() as session, session.begin():
            await set_current_org(session, organization)
            await set_current_user(session, user)
            ctx = await load_project_context(session, organization, uuid.UUID(project_id), user)
            yield Acting(session, ctx, AuditService(self.session_factory))

    @asynccontextmanager
    async def transaction(self, organization_id: str) -> AsyncIterator[AsyncSession]:
        """One transaction as the system, for the workflow's own bookkeeping."""
        async with self.session_factory() as session, session.begin():
            await set_current_org(session, uuid.UUID(organization_id))
            yield session
