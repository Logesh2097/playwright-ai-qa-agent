"""Users, organizations, memberships and credentials of the platform itself."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, INET
from sqlalchemy.orm import Mapped, mapped_column

from app.core.security.permissions import Role
from app.models.base import (
    EMPTY_JSON_OBJECT,
    EMPTY_TEXT_ARRAY,
    Base,
    CreatedAtMixin,
    SoftDeleteMixin,
    TenantMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    one_of,
)

MEMBER_STATUSES = ("invited", "active", "suspended")


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A person. Not tenant-scoped: one user may belong to several organizations."""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(CITEXT, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    full_name: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(server_default=text("true"), default=True)
    is_platform_admin: Mapped[bool] = mapped_column(server_default=text("false"), default=False)
    email_verified_at: Mapped[datetime | None]
    password_changed_at: Mapped[datetime | None]
    last_login_at: Mapped[datetime | None]


class Organization(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "organizations"
    __table_args__ = (
        Index(
            "uq_organizations_slug_active",
            "slug",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    name: Mapped[str] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(CITEXT)
    settings: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON_OBJECT, default=dict)


class OrganizationMember(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    """Membership of a user in an organization, or a pending invitation."""

    __tablename__ = "organization_members"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id"),
        Index(
            "uq_organization_members_pending_invite",
            "organization_id",
            "invited_email",
            unique=True,
            postgresql_where=text("status = 'invited'"),
        ),
        one_of("role", [role.value for role in Role]),
        one_of("status", MEMBER_STATUSES),
        CheckConstraint("user_id IS NOT NULL OR status = 'invited'", name="user_required"),
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    invited_email: Mapped[str | None] = mapped_column(CITEXT)
    role: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="active", default="active")
    invite_token_hash: Mapped[str | None] = mapped_column(Text, unique=True)
    invite_expires_at: Mapped[datetime | None]
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class RefreshToken(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One link in a refresh-token chain. A chain (family) is one login session."""

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    family_id: Mapped[uuid.UUID] = mapped_column(index=True)
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    replaced_by: Mapped[uuid.UUID | None]
    user_agent: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(INET)


class PasswordResetToken(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "password_reset_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]


class ApiKey(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    """Machine credential for CI. Endpoints arrive with the CI integration phase."""

    __tablename__ = "api_keys"

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(Text)
    key_prefix: Mapped[str] = mapped_column(Text)
    key_hash: Mapped[str] = mapped_column(Text, unique=True)
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=EMPTY_TEXT_ARRAY, default=list
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    last_used_at: Mapped[datetime | None]
    expires_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
