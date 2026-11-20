"""Repositories for users, organizations, memberships and auth tokens."""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select, update

from app.core.security.permissions import Role
from app.models import (
    AuditLog,
    Organization,
    OrganizationMember,
    PasswordResetToken,
    RefreshToken,
    User,
)
from app.repositories.base import BaseRepository, TenantRepository


class UserRepository(BaseRepository[User]):
    model = User

    async def get_by_email(self, email: str) -> User | None:
        result = await self._session.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()


class OrganizationRepository(BaseRepository[Organization]):
    model = Organization

    async def get_active(self, organization_id: uuid.UUID) -> Organization | None:
        result = await self._session.execute(
            select(Organization).where(
                Organization.id == organization_id, Organization.deleted_at.is_(None)
            )
        )
        return result.scalar_one_or_none()

    async def list_for_user(
        self, user_id: uuid.UUID
    ) -> Sequence[tuple[Organization, OrganizationMember]]:
        result = await self._session.execute(
            select(Organization, OrganizationMember)
            .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
            .where(
                OrganizationMember.user_id == user_id,
                OrganizationMember.status == "active",
                Organization.deleted_at.is_(None),
            )
            .order_by(Organization.name)
        )
        return [(row[0], row[1]) for row in result.all()]


class MembershipRepository(TenantRepository[OrganizationMember]):
    model = OrganizationMember

    async def get_active_for_user(self, user_id: uuid.UUID) -> OrganizationMember | None:
        result = await self._session.execute(
            self._select().where(
                OrganizationMember.user_id == user_id, OrganizationMember.status == "active"
            )
        )
        return result.scalar_one_or_none()

    async def get_for_user(self, user_id: uuid.UUID) -> OrganizationMember | None:
        result = await self._session.execute(
            self._select().where(OrganizationMember.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def get_pending_invite(self, email: str) -> OrganizationMember | None:
        result = await self._session.execute(
            self._select().where(
                OrganizationMember.invited_email == email, OrganizationMember.status == "invited"
            )
        )
        return result.scalar_one_or_none()

    async def list_with_users(self) -> Sequence[tuple[OrganizationMember, User | None]]:
        result = await self._session.execute(
            select(OrganizationMember, User)
            .outerjoin(User, User.id == OrganizationMember.user_id)
            .where(OrganizationMember.organization_id == self._organization_id)
            .order_by(OrganizationMember.id)
        )
        return [(row[0], row[1]) for row in result.all()]

    async def lock_active_owner_ids(self) -> Sequence[uuid.UUID]:
        """Locks the owner rows so concurrent requests cannot remove the last owner."""
        result = await self._session.execute(
            select(OrganizationMember.id)
            .where(
                OrganizationMember.organization_id == self._organization_id,
                OrganizationMember.role == Role.OWNER.value,
                OrganizationMember.status == "active",
            )
            .with_for_update()
        )
        return list(result.scalars().all())

    async def delete(self, member: OrganizationMember) -> None:
        await self._session.delete(member)
        await self._session.flush()


class InvitationRepository(BaseRepository[OrganizationMember]):
    """Looks an invitation up by token, before the caller belongs to its organization."""

    model = OrganizationMember

    async def get_by_token_hash(self, token_hash: str) -> OrganizationMember | None:
        result = await self._session.execute(
            select(OrganizationMember)
            .where(
                OrganizationMember.invite_token_hash == token_hash,
                OrganizationMember.status == "invited",
            )
            .with_for_update()
        )
        return result.scalar_one_or_none()


class RefreshTokenRepository(BaseRepository[RefreshToken]):
    model = RefreshToken

    async def get_by_hash_for_update(self, token_hash: str) -> RefreshToken | None:
        result = await self._session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
        )
        return result.scalar_one_or_none()

    async def revoke_family(self, family_id: uuid.UUID, now: datetime) -> None:
        await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )

    async def revoke_all_for_user(self, user_id: uuid.UUID, now: datetime) -> Sequence[uuid.UUID]:
        """Revokes every session of the user; returns the affected session IDs."""
        result = await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
            .returning(RefreshToken.family_id)
        )
        return list(set(result.scalars().all()))


class PasswordResetRepository(BaseRepository[PasswordResetToken]):
    model = PasswordResetToken

    async def get_by_hash_for_update(self, token_hash: str) -> PasswordResetToken | None:
        result = await self._session.execute(
            select(PasswordResetToken)
            .where(PasswordResetToken.token_hash == token_hash)
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def invalidate_for_user(self, user_id: uuid.UUID, now: datetime) -> None:
        await self._session.execute(
            update(PasswordResetToken)
            .where(PasswordResetToken.user_id == user_id, PasswordResetToken.used_at.is_(None))
            .values(used_at=now)
        )


class AuditLogRepository(TenantRepository[AuditLog]):
    model = AuditLog

    async def page(
        self,
        *,
        limit: int,
        before_id: uuid.UUID | None = None,
        action: str | None = None,
        actor_id: uuid.UUID | None = None,
    ) -> Sequence[AuditLog]:
        """Newest first. IDs are time-ordered, so the ID is the pagination key."""
        statement = self._select()
        if before_id is not None:
            statement = statement.where(AuditLog.id < before_id)
        if action is not None:
            statement = statement.where(AuditLog.action == action)
        if actor_id is not None:
            statement = statement.where(AuditLog.actor_id == actor_id)
        return await self._all(statement.order_by(AuditLog.id.desc()).limit(limit))
