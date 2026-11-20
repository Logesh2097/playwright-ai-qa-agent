"""What may be tested: projects, applications, environments and their credentials."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    EMPTY_JSON_ARRAY,
    EMPTY_JSON_OBJECT,
    EMPTY_TEXT_ARRAY,
    Base,
    SoftDeleteMixin,
    TenantMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    one_of,
)

PROJECT_STATUSES = ("active", "archived")
PROJECT_MEMBER_ROLES = ("manager", "member")
APPLICATION_TYPES = ("web", "api")
APPLICATION_STATUSES = ("active", "archived")
APPLICATION_AUTH_METHODS = (
    "none",
    "form_login",
    "basic_auth",
    "bearer_token",
    "api_key",
    "sso",
    "other",
)
ENVIRONMENT_KINDS = ("local", "development", "testing", "staging", "production", "other")
ENVIRONMENT_STATUSES = ("active", "disabled")
# Categories of browser/API activity an environment permits. Enforced by the test
# engine and the MCP tool policy (Phases 6-7); validated and stored from Phase 3.
BROWSER_ACTIONS = (
    "navigate",
    "read",
    "click",
    "type",
    "select",
    "upload",
    "download",
    "api_read",
    "api_write",
)
READ_ONLY_ACTIONS = ("navigate", "read", "api_read")
ORIGIN_SCHEMES = ("https", "http")
ORIGIN_PURPOSES = ("app", "api", "auth_provider", "asset")
CREDENTIAL_KINDS = (
    "username_password",
    "bearer_token",
    "api_key_header",
    "basic_auth",
    "cookie",
    "totp_secret",
    "secret_value",
)
# "stored": encrypted in this database. "reference": only a pointer to a secret
# held elsewhere (e.g. env:NAME) is stored; the value never enters the platform's database.
CREDENTIAL_SOURCES = ("stored", "reference")

_NOT_DELETED = text("deleted_at IS NULL")


class Project(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, TenantMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (
        # Target of composite foreign keys: a child row must share the parent's organization.
        UniqueConstraint("id", "organization_id"),
        Index(
            "uq_projects_org_slug_active",
            "organization_id",
            "slug",
            unique=True,
            postgresql_where=_NOT_DELETED,
        ),
        one_of("status", PROJECT_STATUSES),
    )

    name: Mapped[str] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(CITEXT)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="active", default="active")
    settings: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON_OBJECT, default=dict)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class ProjectMember(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    """Grants a user access to one project.

    Organization admins and owners can reach every project without a row here.
    What a member may do is still decided by their organization role; `manager`
    additionally allows editing the project and its member list.
    """

    __tablename__ = "project_members"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "organization_id"],
            ["projects.id", "projects.organization_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("project_id", "user_id"),
        one_of("role", PROJECT_MEMBER_ROLES),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(Text, server_default="member", default="member")
    added_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class Application(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, TenantMixin, Base):
    __tablename__ = "applications"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "organization_id"],
            ["projects.id", "projects.organization_id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "organization_id"),
        Index(
            "uq_applications_project_name_active",
            "project_id",
            "name",
            unique=True,
            postgresql_where=_NOT_DELETED,
        ),
        one_of("type", APPLICATION_TYPES),
        one_of("status", APPLICATION_STATUSES),
        one_of("auth_method", APPLICATION_AUTH_METHODS),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(index=True)
    name: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(Text, server_default="web", default="web")
    description: Mapped[str | None] = mapped_column(Text)
    base_url: Mapped[str] = mapped_column(Text)
    framework: Mapped[str | None] = mapped_column(Text)
    auth_method: Mapped[str] = mapped_column(Text, server_default="none", default="none")
    default_browser_config: Mapped[dict[str, Any]] = mapped_column(
        server_default=EMPTY_JSON_OBJECT, default=dict
    )
    # Hostnames this application may ever be reached on (each covers its subdomains).
    # Every environment URL and allowed origin must fall inside this list.
    allowed_domains: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=EMPTY_TEXT_ARRAY, default=list
    )
    status: Mapped[str] = mapped_column(Text, server_default="active", default="active")
    repo_full_name: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class Environment(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, TenantMixin, Base):
    __tablename__ = "environments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["application_id", "organization_id"],
            ["applications.id", "applications.organization_id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "organization_id"),
        Index(
            "uq_environments_application_name_active",
            "application_id",
            "name",
            unique=True,
            postgresql_where=_NOT_DELETED,
        ),
        one_of("kind", ENVIRONMENT_KINDS),
        one_of("status", ENVIRONMENT_STATUSES),
    )

    application_id: Mapped[uuid.UUID] = mapped_column(index=True)
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    base_url: Mapped[str] = mapped_column(Text)
    api_base_url: Mapped[str | None] = mapped_column(Text)
    is_read_only: Mapped[bool] = mapped_column(server_default=text("false"), default=False)
    requires_approval: Mapped[bool] = mapped_column(server_default=text("false"), default=False)
    allow_private_network: Mapped[bool] = mapped_column(server_default=text("false"), default=False)
    default_viewport: Mapped[dict[str, Any] | None]
    # Non-secret settings, or references to credentials: [{"name", "value"} | {"name", "secret_ref"}]
    variables: Mapped[list[Any]] = mapped_column(server_default=EMPTY_JSON_ARRAY, default=list)
    auth_config: Mapped[dict[str, Any]] = mapped_column(
        server_default=EMPTY_JSON_OBJECT, default=dict
    )
    allowed_actions: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=EMPTY_TEXT_ARRAY, default=list
    )
    status: Mapped[str] = mapped_column(Text, server_default="active", default="active")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class EnvironmentAllowedOrigin(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    """One entry of an environment's URL allowlist."""

    __tablename__ = "environment_allowed_origins"
    __table_args__ = (
        UniqueConstraint("environment_id", "scheme", "host", "port"),
        one_of("scheme", ORIGIN_SCHEMES),
        one_of("purpose", ORIGIN_PURPOSES),
        CheckConstraint("port BETWEEN 1 AND 65535", name="port_range"),
        # A host only: no wildcard, path, credentials or whitespace.
        CheckConstraint(r"host !~ '[*/@\s]' AND host <> ''", name="host_plain"),
    )

    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE"), index=True
    )
    scheme: Mapped[str] = mapped_column(Text)
    host: Mapped[str] = mapped_column(Text)
    port: Mapped[int]
    include_subdomains: Mapped[bool] = mapped_column(server_default=text("false"), default=False)
    purpose: Mapped[str] = mapped_column(Text, server_default="app", default="app")


class Credential(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    """Encrypted secret for logging in to an application under test.

    Only ciphertext is stored. There is deliberately no plaintext column.
    """

    __tablename__ = "credentials"
    __table_args__ = (
        UniqueConstraint("environment_id", "name"),
        one_of("kind", CREDENTIAL_KINDS),
        one_of("source", CREDENTIAL_SOURCES),
        # Exactly one of: an encrypted value, or a reference to an external secret.
        CheckConstraint(
            "(source = 'stored' AND ciphertext IS NOT NULL AND external_ref IS NULL)"
            " OR (source = 'reference' AND ciphertext IS NULL AND external_ref IS NOT NULL)",
            name="secret_location",
        ),
        CheckConstraint("is_test_account", name="test_account_only"),
    )

    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(Text)
    role_label: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, server_default="stored", default="stored")
    ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    wrapped_dek: Mapped[bytes | None] = mapped_column(LargeBinary)
    key_version: Mapped[int | None]
    external_ref: Mapped[str | None] = mapped_column(Text)
    # Names of the fields inside the encrypted payload (e.g. username, password). Not secret.
    field_names: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=EMPTY_TEXT_ARRAY, default=list
    )
    # The person who saved it confirmed this is a dedicated test account.
    is_test_account: Mapped[bool] = mapped_column(server_default=text("true"), default=True)
    setup_instructions: Mapped[str | None] = mapped_column(Text)
    login_recipe: Mapped[list[Any]] = mapped_column(server_default=EMPTY_JSON_ARRAY, default=list)
    last_rotated_at: Mapped[datetime | None]
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
