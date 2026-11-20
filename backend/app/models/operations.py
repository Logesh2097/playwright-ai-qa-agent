"""MCP connections, integrations, notifications and logs."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    LargeBinary,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    EMPTY_JSON_OBJECT,
    EMPTY_TEXT_ARRAY,
    Base,
    CreatedAtMixin,
    TenantMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    one_of,
)

MCP_MODES = ("managed", "external")
MCP_TRANSPORTS = ("streamable_http",)
MCP_BROWSERS = ("chromium", "firefox", "webkit")
MCP_STATUSES = ("active", "disabled", "unhealthy")
INTEGRATION_PROVIDERS = ("github", "webhook", "email")
INTEGRATION_STATUSES = ("pending", "active", "error", "disabled")
NOTIFICATION_CHANNELS = ("in_app", "email", "webhook")
NOTIFICATION_STATUSES = ("pending", "sent", "failed", "read")
AUDIT_ACTOR_TYPES = ("user", "api_key", "system", "agent")
AGENT_EVENT_TYPES = (
    "node_start",
    "node_end",
    "llm_call",
    "tool_call",
    "interrupt",
    "resume",
    "error",
    "budget_exceeded",
)


class McpConnection(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "mcp_connections"
    __table_args__ = (
        UniqueConstraint("organization_id", "name"),
        one_of("mode", MCP_MODES),
        one_of("transport", MCP_TRANSPORTS),
        one_of("browser", MCP_BROWSERS),
        one_of("status", MCP_STATUSES),
    )

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(Text, server_default="managed", default="managed")
    transport: Mapped[str] = mapped_column(
        Text, server_default="streamable_http", default="streamable_http"
    )
    endpoint_url: Mapped[str | None] = mapped_column(Text)
    auth_secret_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    wrapped_dek: Mapped[bytes | None] = mapped_column(LargeBinary)
    key_version: Mapped[int | None]
    allowed_tools: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=EMPTY_TEXT_ARRAY, default=list
    )
    browser: Mapped[str] = mapped_column(Text, server_default="chromium", default="chromium")
    capabilities: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=EMPTY_TEXT_ARRAY, default=list
    )
    status: Mapped[str] = mapped_column(Text, server_default="active", default="active")
    discovered_tools: Mapped[list[Any] | None]
    server_version: Mapped[str | None] = mapped_column(Text)
    last_health_check_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)


class Integration(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "integrations"
    __table_args__ = (
        UniqueConstraint("organization_id", "provider", "installation_id"),
        one_of("provider", INTEGRATION_PROVIDERS),
        one_of("status", INTEGRATION_STATUSES),
    )

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE")
    )
    provider: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="pending", default="pending")
    config: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON_OBJECT, default=dict)
    secret_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    wrapped_dek: Mapped[bytes | None] = mapped_column(LargeBinary)
    key_version: Mapped[int | None]
    external_account: Mapped[str | None] = mapped_column(Text)
    installation_id: Mapped[int | None] = mapped_column(BigInteger)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class Notification(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_user_status_created", "user_id", "status", "created_at"),
        Index(
            "ix_notifications_pending_due",
            "status",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
        one_of("channel", NOTIFICATION_CHANNELS),
        one_of("status", NOTIFICATION_STATUSES),
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(Text)
    subject_type: Mapped[str | None] = mapped_column(Text)
    subject_id: Mapped[uuid.UUID | None]
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON_OBJECT, default=dict)
    status: Mapped[str] = mapped_column(Text, server_default="pending", default="pending")
    attempts: Mapped[int] = mapped_column(server_default=text("0"), default=0)
    next_attempt_at: Mapped[datetime | None]
    sent_at: Mapped[datetime | None]
    read_at: Mapped[datetime | None]


class AuditLog(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Append-only record of security-relevant actions.

    `organization_id` is nullable: some events (a failed login for an unknown
    account) belong to no organization. The application role may only INSERT
    and SELECT on this table.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_org_id", "organization_id", "id"),
        Index("ix_audit_logs_resource", "resource_type", "resource_id"),
        one_of("actor_type", AUDIT_ACTOR_TYPES),
    )

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    actor_type: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[uuid.UUID | None]
    action: Mapped[str] = mapped_column(Text)
    resource_type: Mapped[str | None] = mapped_column(Text)
    resource_id: Mapped[uuid.UUID | None]
    request_id: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    changes: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON_OBJECT, default=dict)


class AgentExecutionLog(UUIDPrimaryKeyMixin, CreatedAtMixin, TenantMixin, Base):
    """Trace of the agent: node transitions, LLM calls and tool calls."""

    __tablename__ = "agent_execution_logs"
    __table_args__ = (
        Index("ix_agent_execution_logs_execution_created", "execution_id", "created_at"),
        one_of("event_type", AGENT_EVENT_TYPES),
    )

    thread_id: Mapped[str] = mapped_column(Text)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("test_executions.id", ondelete="CASCADE")
    )
    instruction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("test_instructions.id", ondelete="CASCADE")
    )
    node: Mapped[str | None] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    prompt_tokens: Mapped[int | None]
    completion_tokens: Mapped[int | None]
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    latency_ms: Mapped[int | None]
    tool_name: Mapped[str | None] = mapped_column(Text)
    tool_args: Mapped[dict[str, Any] | None]
    tool_result_summary: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    error: Mapped[dict[str, Any] | None]


GITHUB_SELECTION_MODES = ("all", "affected")
GITHUB_DELIVERY_OUTCOMES = ("accepted", "ignored", "duplicate", "failed", "processed")


class GithubRepository(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    """One GitHub repository a project has explicitly been linked to.

    Nothing is ever read from or written to a repository that has no row here.
    The row also says what may happen automatically: which events start tests,
    for which branches, where, and whether anything is written back.
    """

    __tablename__ = "github_repositories"
    __table_args__ = (
        UniqueConstraint("project_id", "github_repo_id"),
        one_of("selection_mode", GITHUB_SELECTION_MODES),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    integration_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("integrations.id", ondelete="CASCADE")
    )
    # GitHub's own numeric ID: stays the same when a repository is renamed or transferred.
    github_repo_id: Mapped[int] = mapped_column(BigInteger)
    full_name: Mapped[str] = mapped_column(Text)
    default_branch: Mapped[str] = mapped_column(Text)
    is_private: Mapped[bool] = mapped_column(server_default=text("true"), default=True)
    # Random, in the webhook address. Says which row a delivery is for; proves nothing.
    hook_token: Mapped[str] = mapped_column(Text, unique=True)
    # The webhook secret, encrypted like a credential.
    secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary)
    key_version: Mapped[int]
    enabled: Mapped[bool] = mapped_column(server_default=text("true"), default=True)
    on_pull_request: Mapped[bool] = mapped_column(server_default=text("false"), default=False)
    on_push: Mapped[bool] = mapped_column(server_default=text("false"), default=False)
    # Branch names or patterns (`release/*`). For pull requests: the branch merged into.
    branches: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=EMPTY_TEXT_ARRAY, default=list
    )
    environment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("environments.id", ondelete="SET NULL")
    )
    # The approved suites that make up a full run.
    suite_ids: Mapped[list[Any]] = mapped_column(server_default=text("'[]'::jsonb"), default=list)
    selection_mode: Mapped[str] = mapped_column(Text, server_default="all", default="all")
    # [{"pattern": "src/billing/**", "suite_ids": ["…"]}]: which suites a changed path concerns.
    path_rules: Mapped[list[Any]] = mapped_column(server_default=text("'[]'::jsonb"), default=list)
    status_enabled: Mapped[bool] = mapped_column(server_default=text("false"), default=False)
    comment_enabled: Mapped[bool] = mapped_column(server_default=text("false"), default=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class GithubDelivery(UUIDPrimaryKeyMixin, CreatedAtMixin, TenantMixin, Base):
    """One webhook delivery that passed signature verification, and what was done with it."""

    __tablename__ = "github_deliveries"
    __table_args__ = (
        UniqueConstraint("repository_id", "delivery_id"),
        Index("ix_github_deliveries_repository_created", "repository_id", "created_at"),
        one_of("outcome", GITHUB_DELIVERY_OUTCOMES),
    )

    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("github_repositories.id", ondelete="CASCADE")
    )
    # GitHub's X-GitHub-Delivery header: the same for every redelivery of one event.
    delivery_id: Mapped[str] = mapped_column(Text)
    event: Mapped[str] = mapped_column(Text)
    action: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    # What the event was about: pull request number, commit, branches. No payload is kept.
    subject: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON_OBJECT, default=dict)
    execution_ids: Mapped[list[Any]] = mapped_column(
        server_default=text("'[]'::jsonb"), default=list
    )
