"""Organizations, memberships and invitations."""

import re
import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.core.security.permissions import Permission, Role, can_manage_role, has_permission
from app.core.security.tokens import generate_opaque_token, hash_token
from app.db.context import set_current_org, set_invite_token_hash
from app.db.errors import is_unique_violation
from app.models import AuditLog, Organization, OrganizationMember, User
from app.repositories.identity import (
    AuditLogRepository,
    InvitationRepository,
    MembershipRepository,
    OrganizationRepository,
    UserRepository,
)
from app.repositories.projects import ProjectMemberRepository
from app.services.audit import AuditEvent, AuditService
from app.services.email import EmailSender, invitation_email
from app.utils.ids import uuid7

_SLUG_INVALID = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class OrgContext:
    """The caller's verified position inside one organization."""

    user: User
    organization: Organization
    membership: OrganizationMember

    @property
    def role(self) -> Role:
        return Role(self.membership.role)

    def require(self, permission: Permission) -> None:
        if not has_permission(self.role, permission):
            raise ForbiddenError("Your role does not allow this action.", code="insufficient_role")


def slugify(name: str) -> str:
    slug = _SLUG_INVALID.sub("-", name.lower()).strip("-")[:48].strip("-")
    return slug or "org"


class OrganizationService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        audit: AuditService,
        email: EmailSender,
    ) -> None:
        self._session = session
        self._settings = settings
        self._audit = audit
        self._email = email
        self._organizations = OrganizationRepository(session)

    # --- organizations -----------------------------------------------------------

    async def list_for_user(self, user: User) -> Sequence[tuple[Organization, OrganizationMember]]:
        return await self._organizations.list_for_user(user.id)

    async def create(self, user: User, name: str) -> Organization:
        """Create an organization with `user` as its owner."""
        organization = Organization(id=uuid7(), name=name, slug=slugify(name))
        # Row-level security only admits rows of the current organization, so the
        # context has to point at the new organization before it is inserted.
        await set_current_org(self._session, organization.id)
        try:
            # Slugs are unique across all organizations, but other tenants' rows are
            # invisible here, so a clash can only be detected by trying the insert.
            async with self._session.begin_nested():
                await self._organizations.add(organization)
        except IntegrityError as exc:
            if not is_unique_violation(exc):
                raise
            organization = Organization(
                id=organization.id, name=name, slug=f"{slugify(name)}-{secrets.token_hex(4)}"
            )
            await self._organizations.add(organization)
        await MembershipRepository(self._session, organization.id).add(
            OrganizationMember(
                organization_id=organization.id,
                user_id=user.id,
                role=Role.OWNER.value,
                status="active",
            )
        )
        await self._audit.record(
            self._session,
            AuditEvent(
                action="organization.create",
                actor_id=user.id,
                organization_id=organization.id,
                resource_type="organization",
                resource_id=organization.id,
                changes={"name": name},
            ),
        )
        return organization

    async def update(self, ctx: OrgContext, *, name: str) -> Organization:
        ctx.require(Permission.ORG_UPDATE)
        previous = ctx.organization.name
        ctx.organization.name = name
        await self._audit.record(
            self._session,
            self._event(ctx, "organization.update", changes={"name": [previous, name]}),
        )
        await self._session.flush()
        return ctx.organization

    async def delete(self, ctx: OrgContext) -> None:
        ctx.require(Permission.ORG_DELETE)
        ctx.organization.deleted_at = datetime.now(UTC)
        await self._audit.record(self._session, self._event(ctx, "organization.delete"))

    # --- members -----------------------------------------------------------------

    async def list_members(
        self, ctx: OrgContext
    ) -> Sequence[tuple[OrganizationMember, User | None]]:
        ctx.require(Permission.MEMBERS_READ)
        return await MembershipRepository(self._session, ctx.organization.id).list_with_users()

    async def invite(self, ctx: OrgContext, *, email: str, role: Role) -> OrganizationMember:
        ctx.require(Permission.MEMBERS_MANAGE)
        self._require_can_manage(ctx, role)
        members = MembershipRepository(self._session, ctx.organization.id)

        existing_user = await UserRepository(self._session).get_by_email(email)
        if existing_user and await members.get_for_user(existing_user.id):
            raise ConflictError("This person is already a member.", code="already_member")
        if await members.get_pending_invite(email):
            raise ConflictError("This person has already been invited.", code="already_invited")

        token = generate_opaque_token()
        invitation = await members.add(
            OrganizationMember(
                organization_id=ctx.organization.id,
                invited_email=email,
                role=role.value,
                status="invited",
                invite_token_hash=hash_token(token),
                invite_expires_at=datetime.now(UTC)
                + timedelta(seconds=self._settings.invitation_ttl_s),
                invited_by=ctx.user.id,
            )
        )
        await self._audit.record(
            self._session,
            self._event(
                ctx,
                "member.invite",
                resource_type="organization_member",
                resource_id=invitation.id,
                changes={"email": email, "role": role.value},
            ),
        )
        await self._email.send(
            invitation_email(
                to=email,
                base_url=self._settings.public_base_url,
                token=token,
                organization_name=ctx.organization.name,
                inviter_name=ctx.user.full_name,
            )
        )
        return invitation

    async def accept_invitation(self, user: User, token: str) -> OrganizationMember:
        """Turn a pending invitation into an active membership of `user`."""
        invalid = BadRequestError(
            "The invitation is invalid or has expired.", code="invalid_invitation"
        )
        token_hash = hash_token(token)
        await set_invite_token_hash(self._session, token_hash)
        invitation = await InvitationRepository(self._session).get_by_token_hash(token_hash)
        if (
            invitation is None
            or invitation.invite_expires_at is None
            or invitation.invite_expires_at <= datetime.now(UTC)
            # The invitation is bound to the address it was sent to.
            or not secrets.compare_digest(
                (invitation.invited_email or "").lower(), user.email.lower()
            )
        ):
            raise invalid

        await set_current_org(self._session, invitation.organization_id)
        if await self._organizations.get_active(invitation.organization_id) is None:
            raise invalid
        members = MembershipRepository(self._session, invitation.organization_id)
        if await members.get_for_user(user.id) is not None:
            raise ConflictError("You are already a member.", code="already_member")

        invitation.user_id = user.id
        invitation.status = "active"
        invitation.invite_token_hash = None
        invitation.invite_expires_at = None
        await self._session.flush()
        await self._audit.record(
            self._session,
            AuditEvent(
                action="member.join",
                actor_id=user.id,
                organization_id=invitation.organization_id,
                resource_type="organization_member",
                resource_id=invitation.id,
                changes={"role": invitation.role},
            ),
        )
        return invitation

    async def change_role(
        self, ctx: OrgContext, member_id: uuid.UUID, role: Role
    ) -> tuple[OrganizationMember, User | None]:
        ctx.require(Permission.MEMBERS_MANAGE)
        members = MembershipRepository(self._session, ctx.organization.id)
        owner_ids = await members.lock_active_owner_ids()
        member = await self._get_member(members, member_id)
        current = Role(member.role)

        self._require_can_manage(ctx, current)
        self._require_can_manage(ctx, role)
        if current is Role.OWNER and role is not Role.OWNER and owner_ids == [member.id]:
            raise ConflictError("An organization must keep at least one owner.", code="last_owner")

        member.role = role.value
        await self._session.flush()
        await self._audit.record(
            self._session,
            self._event(
                ctx,
                "member.role_change",
                resource_type="organization_member",
                resource_id=member.id,
                changes={"role": [current.value, role.value]},
            ),
        )
        user = await UserRepository(self._session).get(member.user_id) if member.user_id else None
        return member, user

    async def remove_member(self, ctx: OrgContext, member_id: uuid.UUID) -> None:
        """Remove a member or cancel an invitation. Anyone may remove themselves."""
        members = MembershipRepository(self._session, ctx.organization.id)
        owner_ids = await members.lock_active_owner_ids()
        member = await self._get_member(members, member_id)
        is_self = member.user_id == ctx.user.id

        if not is_self:
            ctx.require(Permission.MEMBERS_MANAGE)
            self._require_can_manage(ctx, Role(member.role))
        if owner_ids == [member.id]:
            raise ConflictError("An organization must keep at least one owner.", code="last_owner")

        await self._audit.record(
            self._session,
            self._event(
                ctx,
                "member.leave" if is_self else "member.remove",
                resource_type="organization_member",
                resource_id=member.id,
                changes={"role": member.role, "status": member.status},
            ),
        )
        if member.user_id is not None:
            # Leaving the organization ends access to all of its projects.
            await ProjectMemberRepository(
                self._session, ctx.organization.id
            ).remove_user_everywhere(member.user_id)
        await members.delete(member)

    # --- audit log ---------------------------------------------------------------

    async def audit_page(
        self,
        ctx: OrgContext,
        *,
        limit: int,
        cursor: uuid.UUID | None,
        action: str | None,
        actor_id: uuid.UUID | None,
    ) -> tuple[Sequence[AuditLog], uuid.UUID | None]:
        ctx.require(Permission.AUDIT_READ)
        rows = await AuditLogRepository(self._session, ctx.organization.id).page(
            limit=limit + 1, before_id=cursor, action=action, actor_id=actor_id
        )
        page = rows[:limit]
        next_cursor = page[-1].id if len(rows) > limit and page else None
        return page, next_cursor

    # --- helpers -----------------------------------------------------------------

    @staticmethod
    def _require_can_manage(ctx: OrgContext, target: Role) -> None:
        if not can_manage_role(ctx.role, target):
            raise ForbiddenError(
                "You cannot assign or change a role above your own.", code="insufficient_role"
            )

    @staticmethod
    async def _get_member(
        members: MembershipRepository, member_id: uuid.UUID
    ) -> OrganizationMember:
        member = await members.get(member_id)
        if member is None:
            raise NotFoundError("Member not found.")
        return member

    @staticmethod
    def _event(
        ctx: OrgContext,
        action: str,
        *,
        resource_type: str = "organization",
        resource_id: uuid.UUID | None = None,
        changes: dict[str, object] | None = None,
    ) -> AuditEvent:
        return AuditEvent(
            action=action,
            actor_id=ctx.user.id,
            organization_id=ctx.organization.id,
            resource_type=resource_type,
            resource_id=resource_id or ctx.organization.id,
            changes=changes or {},
        )
