"""Projects and project membership."""

import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    ConflictError,
    ErrorDetail,
    ForbiddenError,
    NotFoundError,
    ValidationFailedError,
)
from app.core.security.permissions import Permission, has_permission
from app.db.errors import is_unique_violation
from app.models import Project, ProjectMember, User
from app.repositories.identity import MembershipRepository
from app.repositories.projects import ProjectMemberRepository, ProjectRepository
from app.schemas.projects import ProjectCreate, ProjectUpdate
from app.services.audit import AuditEvent, AuditService
from app.services.organizations import OrgContext, slugify


@dataclass(frozen=True, slots=True)
class ProjectContext:
    """The caller's verified position inside one project."""

    org: OrgContext
    project: Project
    membership: ProjectMember | None

    @property
    def user(self) -> User:
        return self.org.user

    @property
    def organization_id(self) -> uuid.UUID:
        return self.org.organization.id

    @property
    def can_manage(self) -> bool:
        """Organization admins manage every project; otherwise the project's managers do."""
        if has_permission(self.org.role, Permission.PROJECTS_ACCESS_ALL):
            return True
        return (
            self.membership is not None
            and self.membership.role == "manager"
            and has_permission(self.org.role, Permission.PROJECTS_CREATE)
        )

    def require(self, permission: Permission) -> None:
        self.org.require(permission)

    def require_manager(self) -> None:
        if not self.can_manage:
            raise ForbiddenError(
                "Only project managers and organization admins can do this.",
                code="insufficient_role",
            )

    def require_writable(self) -> None:
        if self.project.status != "active":
            raise ConflictError(
                "This project is archived. Restore it to make changes.", code="project_archived"
            )


@dataclass(frozen=True, slots=True)
class ProjectView:
    project: Project
    member_count: int
    application_count: int
    my_role: str | None
    can_manage: bool


class ProjectService:
    def __init__(self, *, session: AsyncSession, audit: AuditService) -> None:
        self._session = session
        self._audit = audit

    def _projects(self, org: OrgContext) -> ProjectRepository:
        return ProjectRepository(self._session, org.organization.id)

    def _members(self, org: OrgContext) -> ProjectMemberRepository:
        return ProjectMemberRepository(self._session, org.organization.id)

    # --- access ------------------------------------------------------------------

    async def resolve(self, org: OrgContext, project_id: uuid.UUID) -> ProjectContext:
        """Load a project the caller may see. Anything else is a 404."""
        org.require(Permission.PROJECTS_READ)
        project = await self._projects(org).get(project_id)
        if project is None:
            raise NotFoundError("Project not found.")
        membership = await self._members(org).get_for_user(project.id, org.user.id)
        if membership is None and not has_permission(org.role, Permission.PROJECTS_ACCESS_ALL):
            # Same answer as for a project that does not exist.
            raise NotFoundError("Project not found.")
        return ProjectContext(org=org, project=project, membership=membership)

    async def view(self, ctx: ProjectContext) -> ProjectView:
        counts = await self._projects(ctx.org).counts([ctx.project.id])
        members, applications = counts[ctx.project.id]
        return ProjectView(
            project=ctx.project,
            member_count=members,
            application_count=applications,
            my_role=ctx.membership.role if ctx.membership else None,
            can_manage=ctx.can_manage,
        )

    async def list(self, org: OrgContext) -> list[ProjectView]:
        org.require(Permission.PROJECTS_READ)
        sees_all = has_permission(org.role, Permission.PROJECTS_ACCESS_ALL)
        projects = await self._projects(org).list_visible(user_id=None if sees_all else org.user.id)
        counts = await self._projects(org).counts([project.id for project in projects])
        views = []
        for project in projects:
            membership = await self._members(org).get_for_user(project.id, org.user.id)
            ctx = ProjectContext(org=org, project=project, membership=membership)
            views.append(
                ProjectView(
                    project=project,
                    member_count=counts[project.id][0],
                    application_count=counts[project.id][1],
                    my_role=membership.role if membership else None,
                    can_manage=ctx.can_manage,
                )
            )
        return views

    # --- lifecycle ---------------------------------------------------------------

    async def create(self, org: OrgContext, data: ProjectCreate) -> ProjectContext:
        org.require(Permission.PROJECTS_CREATE)
        projects = self._projects(org)
        slug = slugify(data.name)
        if await projects.slug_taken(slug):
            slug = f"{slug}-{secrets.token_hex(3)}"
        project = Project(
            organization_id=org.organization.id,
            name=data.name,
            slug=slug,
            description=data.description,
            created_by=org.user.id,
        )
        await projects.add(project)
        membership = await self._members(org).add(
            ProjectMember(
                organization_id=org.organization.id,
                project_id=project.id,
                user_id=org.user.id,
                role="manager",
                added_by=org.user.id,
            )
        )
        ctx = ProjectContext(org=org, project=project, membership=membership)
        await self._record(ctx, "project.create", changes={"name": data.name})
        return ctx

    async def update(self, ctx: ProjectContext, data: ProjectUpdate) -> None:
        ctx.require_manager()
        ctx.require_writable()
        changes: dict[str, object] = {}
        if data.name is not None and data.name != ctx.project.name:
            changes["name"] = [ctx.project.name, data.name]
            ctx.project.name = data.name
        if "description" in data.model_fields_set and data.description != ctx.project.description:
            changes["description"] = "changed"
            ctx.project.description = data.description
        if changes:
            await self._session.flush()
            await self._record(ctx, "project.update", changes=changes)

    async def set_archived(self, ctx: ProjectContext, archived: bool) -> None:
        ctx.require_manager()
        target = "archived" if archived else "active"
        if ctx.project.status == target:
            return
        ctx.project.status = target
        await self._session.flush()
        await self._record(ctx, "project.archive" if archived else "project.restore")

    async def delete(self, ctx: ProjectContext, *, confirm_name: str) -> None:
        """Soft-delete a project. Deliberately hard to do by accident."""
        ctx.require(Permission.PROJECTS_DELETE)
        if ctx.project.status != "archived":
            raise ConflictError(
                "Archive the project before deleting it.", code="project_not_archived"
            )
        if confirm_name != ctx.project.name:
            raise ValidationFailedError(
                "Type the project's exact name to confirm.",
                code="confirmation_mismatch",
                errors=[
                    ErrorDetail(field="confirm_name", code="mismatch", message="does not match")
                ],
            )
        projects = self._projects(ctx.org)
        if await projects.has_active_executions(ctx.project.id):
            raise ConflictError(
                "Tests are still running in this project.", code="project_has_active_executions"
            )
        now = datetime.now(UTC)
        await projects.soft_delete_children(ctx.project.id, now)
        ctx.project.deleted_at = now
        await self._session.flush()
        await self._record(ctx, "project.delete", changes={"name": ctx.project.name})

    # --- members -----------------------------------------------------------------

    async def list_members(self, ctx: ProjectContext) -> Sequence[tuple[ProjectMember, User, str]]:
        return await self._members(ctx.org).list_with_users(ctx.project.id)

    async def add_member(
        self, ctx: ProjectContext, *, user_id: uuid.UUID, role: str
    ) -> ProjectMember:
        ctx.require_manager()
        ctx.require_writable()
        org_membership = await MembershipRepository(
            self._session, ctx.organization_id
        ).get_active_for_user(user_id)
        if org_membership is None:
            # Also the answer for users of other organizations: their existence is not confirmed.
            raise ValidationFailedError(
                "This person is not an active member of the organization.",
                code="not_an_organization_member",
            )
        try:
            async with self._session.begin_nested():
                member = await self._members(ctx.org).add(
                    ProjectMember(
                        organization_id=ctx.organization_id,
                        project_id=ctx.project.id,
                        user_id=user_id,
                        role=role,
                        added_by=ctx.user.id,
                    )
                )
        except IntegrityError as exc:
            if is_unique_violation(exc):
                raise ConflictError(
                    "This person is already in the project.", code="already_member"
                ) from exc
            raise
        await self._record(
            ctx, "project.member_add", changes={"user_id": str(user_id), "role": role}
        )
        return member

    async def change_member_role(
        self, ctx: ProjectContext, *, user_id: uuid.UUID, role: str
    ) -> ProjectMember:
        ctx.require_manager()
        member = await self._get_member(ctx, user_id)
        if member.role != role:
            previous, member.role = member.role, role
            await self._session.flush()
            await self._record(
                ctx,
                "project.member_role_change",
                changes={"user_id": str(user_id), "role": [previous, role]},
            )
        return member

    async def remove_member(self, ctx: ProjectContext, *, user_id: uuid.UUID) -> None:
        """Managers remove others; anyone may remove themselves."""
        if user_id != ctx.user.id:
            ctx.require_manager()
        member = await self._get_member(ctx, user_id)
        await self._members(ctx.org).delete(member)
        await self._record(ctx, "project.member_remove", changes={"user_id": str(user_id)})

    # --- helpers -----------------------------------------------------------------

    async def _get_member(self, ctx: ProjectContext, user_id: uuid.UUID) -> ProjectMember:
        member = await self._members(ctx.org).get_for_user(ctx.project.id, user_id)
        if member is None:
            raise NotFoundError("Project member not found.")
        return member

    async def _record(
        self, ctx: ProjectContext, action: str, *, changes: dict[str, object] | None = None
    ) -> None:
        await self._audit.record(
            self._session,
            AuditEvent(
                action=action,
                actor_id=ctx.user.id,
                organization_id=ctx.organization_id,
                resource_type="project",
                resource_id=ctx.project.id,
                changes=changes or {},
            ),
        )
