"""Repositories for projects, applications, environments and credentials."""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Result, delete, func, select, update

from app.models import (
    Application,
    Credential,
    Environment,
    EnvironmentAllowedOrigin,
    OrganizationMember,
    Project,
    ProjectMember,
    TestExecution,
    User,
)
from app.models.execution import ACTIVE_EXECUTION_STATUSES
from app.repositories.base import TenantRepository


def _pairs(result: Result[uuid.UUID, int]) -> dict[uuid.UUID, int]:
    """Turn (id, count) rows into a dictionary."""
    return {row[0]: row[1] for row in result.all()}


class ProjectRepository(TenantRepository[Project]):
    model = Project

    async def count(self) -> int:
        result = await self._session.execute(
            select(func.count()).select_from(self._select().subquery())
        )
        return int(result.scalar_one())

    async def list_visible(self, *, user_id: uuid.UUID | None) -> Sequence[Project]:
        """All projects, or only those `user_id` is a member of."""
        statement = self._select()
        if user_id is not None:
            statement = statement.where(
                Project.id.in_(
                    select(ProjectMember.project_id).where(ProjectMember.user_id == user_id)
                )
            )
        return await self._all(statement.order_by(Project.name))

    async def slug_taken(self, slug: str) -> bool:
        result = await self._session.execute(self._select().where(Project.slug == slug))
        return result.first() is not None

    async def counts(self, project_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, tuple[int, int]]:
        """(member count, application count) per project."""
        if not project_ids:
            return {}
        members = _pairs(
            await self._session.execute(
                select(ProjectMember.project_id, func.count())
                .where(ProjectMember.project_id.in_(project_ids))
                .group_by(ProjectMember.project_id)
            )
        )
        applications = _pairs(
            await self._session.execute(
                select(Application.project_id, func.count())
                .where(Application.project_id.in_(project_ids), Application.deleted_at.is_(None))
                .group_by(Application.project_id)
            )
        )
        return {pid: (members.get(pid, 0), applications.get(pid, 0)) for pid in project_ids}

    async def has_active_executions(self, project_id: uuid.UUID) -> bool:
        result = await self._session.execute(
            select(TestExecution.id)
            .where(
                TestExecution.organization_id == self._organization_id,
                TestExecution.project_id == project_id,
                TestExecution.status.in_(ACTIVE_EXECUTION_STATUSES),
            )
            .limit(1)
        )
        return result.first() is not None

    async def soft_delete_children(self, project_id: uuid.UUID, now: datetime) -> None:
        """Mark applications and environments deleted and destroy their credentials."""
        org = self._organization_id
        application_ids = select(Application.id).where(
            Application.organization_id == org, Application.project_id == project_id
        )
        environment_ids = select(Environment.id).where(
            Environment.organization_id == org, Environment.application_id.in_(application_ids)
        )
        # Secrets do not outlive the project, even though other rows are kept.
        await self._session.execute(
            delete(Credential).where(
                Credential.organization_id == org, Credential.environment_id.in_(environment_ids)
            )
        )
        await self._session.execute(
            update(Environment)
            .where(Environment.organization_id == org, Environment.id.in_(environment_ids))
            .values(deleted_at=now)
        )
        await self._session.execute(
            update(Application)
            .where(Application.organization_id == org, Application.project_id == project_id)
            .values(deleted_at=now)
        )


class ProjectMemberRepository(TenantRepository[ProjectMember]):
    model = ProjectMember

    async def get_for_user(self, project_id: uuid.UUID, user_id: uuid.UUID) -> ProjectMember | None:
        result = await self._session.execute(
            self._select().where(
                ProjectMember.project_id == project_id, ProjectMember.user_id == user_id
            )
        )
        return result.scalar_one_or_none()

    async def list_with_users(
        self, project_id: uuid.UUID
    ) -> Sequence[tuple[ProjectMember, User, str]]:
        result = await self._session.execute(
            select(ProjectMember, User, OrganizationMember.role)
            .join(User, User.id == ProjectMember.user_id)
            .join(
                OrganizationMember,
                (OrganizationMember.user_id == ProjectMember.user_id)
                & (OrganizationMember.organization_id == ProjectMember.organization_id),
            )
            .where(
                ProjectMember.organization_id == self._organization_id,
                ProjectMember.project_id == project_id,
            )
            .order_by(User.full_name)
        )
        return [(row[0], row[1], row[2]) for row in result.all()]

    async def delete(self, member: ProjectMember) -> None:
        await self._session.delete(member)
        await self._session.flush()

    async def remove_user_everywhere(self, user_id: uuid.UUID) -> None:
        """Called when someone leaves the organization: their project access goes too."""
        await self._session.execute(
            delete(ProjectMember).where(
                ProjectMember.organization_id == self._organization_id,
                ProjectMember.user_id == user_id,
            )
        )


class ApplicationRepository(TenantRepository[Application]):
    model = Application

    async def get_in_project(
        self, project_id: uuid.UUID, application_id: uuid.UUID
    ) -> Application | None:
        result = await self._session.execute(
            self._select().where(
                Application.id == application_id, Application.project_id == project_id
            )
        )
        return result.scalar_one_or_none()

    async def list_in_project(self, project_id: uuid.UUID) -> Sequence[Application]:
        return await self._all(
            self._select().where(Application.project_id == project_id).order_by(Application.name)
        )

    async def environment_counts(
        self, application_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        if not application_ids:
            return {}
        result = await self._session.execute(
            select(Environment.application_id, func.count())
            .where(
                Environment.application_id.in_(application_ids), Environment.deleted_at.is_(None)
            )
            .group_by(Environment.application_id)
        )
        return _pairs(result)


class EnvironmentRepository(TenantRepository[Environment]):
    model = Environment

    async def get_in_application(
        self, application_id: uuid.UUID, environment_id: uuid.UUID
    ) -> Environment | None:
        result = await self._session.execute(
            self._select().where(
                Environment.id == environment_id, Environment.application_id == application_id
            )
        )
        return result.scalar_one_or_none()

    async def list_in_application(self, application_id: uuid.UUID) -> Sequence[Environment]:
        return await self._all(
            self._select()
            .where(Environment.application_id == application_id)
            .order_by(Environment.name)
        )

    async def origins(
        self, environment_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[EnvironmentAllowedOrigin]]:
        grouped: dict[uuid.UUID, list[EnvironmentAllowedOrigin]] = {e: [] for e in environment_ids}
        if not environment_ids:
            return grouped
        result = await self._session.execute(
            select(EnvironmentAllowedOrigin)
            .where(
                EnvironmentAllowedOrigin.organization_id == self._organization_id,
                EnvironmentAllowedOrigin.environment_id.in_(environment_ids),
            )
            .order_by(EnvironmentAllowedOrigin.host, EnvironmentAllowedOrigin.port)
        )
        for origin in result.scalars():
            grouped[origin.environment_id].append(origin)
        return grouped

    async def replace_origins(
        self, environment_id: uuid.UUID, origins: Sequence[EnvironmentAllowedOrigin]
    ) -> None:
        await self._session.execute(
            delete(EnvironmentAllowedOrigin).where(
                EnvironmentAllowedOrigin.organization_id == self._organization_id,
                EnvironmentAllowedOrigin.environment_id == environment_id,
            )
        )
        self._session.add_all(origins)
        await self._session.flush()

    async def credential_counts(self, environment_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
        if not environment_ids:
            return {}
        result = await self._session.execute(
            select(Credential.environment_id, func.count())
            .where(
                Credential.organization_id == self._organization_id,
                Credential.environment_id.in_(environment_ids),
            )
            .group_by(Credential.environment_id)
        )
        return _pairs(result)


class CredentialRepository(TenantRepository[Credential]):
    model = Credential

    async def get_in_environment(
        self, environment_id: uuid.UUID, credential_id: uuid.UUID
    ) -> Credential | None:
        result = await self._session.execute(
            self._select().where(
                Credential.id == credential_id, Credential.environment_id == environment_id
            )
        )
        return result.scalar_one_or_none()

    async def list_in_environment(self, environment_id: uuid.UUID) -> Sequence[Credential]:
        return await self._all(
            self._select()
            .where(Credential.environment_id == environment_id)
            .order_by(Credential.name)
        )

    async def names_in_environment(self, environment_id: uuid.UUID) -> set[str]:
        result = await self._session.execute(
            select(Credential.name).where(
                Credential.organization_id == self._organization_id,
                Credential.environment_id == environment_id,
            )
        )
        return set(result.scalars().all())

    async def delete(self, credential: Credential) -> None:
        await self._session.delete(credential)
        await self._session.flush()

    async def delete_for_environment(self, environment_id: uuid.UUID) -> None:
        await self._session.execute(
            delete(Credential).where(
                Credential.organization_id == self._organization_id,
                Credential.environment_id == environment_id,
            )
        )
