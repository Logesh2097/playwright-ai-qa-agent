"""Agent workflow runs and their LangGraph checkpoints."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    EMPTY_JSON_ARRAY,
    EMPTY_JSON_OBJECT,
    Base,
    CreatedAtMixin,
    TenantMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    one_of,
)

WORKFLOW_MODES = ("generate_and_run", "run_existing")
WORKFLOW_STATUSES = (
    "queued",
    "running",
    "waiting_for_clarification",
    "waiting_for_approval",
    "cancelling",
    "completed",
    "failed",
    "rejected",
    "cancelled",
)
ACTIVE_WORKFLOW_STATUSES = ("queued", "running", "cancelling")
WAITING_WORKFLOW_STATUSES = ("waiting_for_clarification", "waiting_for_approval")
ENDED_WORKFLOW_STATUSES = ("completed", "failed", "rejected", "cancelled")


class WorkflowRun(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    """One run of the agent workflow. Its ID is the LangGraph thread ID."""

    __tablename__ = "workflow_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "organization_id"],
            ["projects.id", "projects.organization_id"],
            ondelete="RESTRICT",
        ),
        Index("ix_workflow_runs_project_created", "project_id", "created_at"),
        Index("ix_workflow_runs_org_status", "organization_id", "status"),
        one_of("mode", WORKFLOW_MODES),
        one_of("status", WORKFLOW_STATUSES),
    )

    project_id: Mapped[uuid.UUID]
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="RESTRICT")
    )
    instruction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("test_instructions.id", ondelete="SET NULL")
    )
    suite_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("test_suites.id", ondelete="SET NULL")
    )
    mode: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="queued", default="queued")
    # The node that is running, or that the run is waiting in.
    current_node: Mapped[str | None] = mapped_column(Text)
    started_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    options: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON_OBJECT, default=dict)
    # Every execution the run started, in order: the first attempt and each retry.
    execution_ids: Mapped[list[Any]] = mapped_column(server_default=EMPTY_JSON_ARRAY, default=list)
    retry_count: Mapped[int] = mapped_column(server_default=text("0"), default=0)
    verdict: Mapped[str | None] = mapped_column(Text)
    # What the run is waiting for: questions, or the reasons an approval is needed.
    pending: Mapped[dict[str, Any] | None]
    error: Mapped[dict[str, Any] | None]
    report: Mapped[dict[str, Any] | None]
    report_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("test_reports.id", ondelete="SET NULL")
    )
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    # Updated whenever a node starts or ends. A running run with an old heartbeat was interrupted.
    heartbeat_at: Mapped[datetime | None]


class WorkflowCheckpoint(UUIDPrimaryKeyMixin, CreatedAtMixin, TenantMixin, Base):
    """A LangGraph checkpoint: the workflow state after a step, serialized by LangGraph."""

    __tablename__ = "workflow_checkpoints"
    __table_args__ = (UniqueConstraint("thread_id", "checkpoint_ns", "checkpoint_id"),)

    thread_id: Mapped[str] = mapped_column(Text)
    checkpoint_ns: Mapped[str] = mapped_column(Text, server_default="", default="")
    checkpoint_id: Mapped[str] = mapped_column(Text)
    parent_checkpoint_id: Mapped[str | None] = mapped_column(Text)
    checkpoint_type: Mapped[str] = mapped_column(Text)
    checkpoint: Mapped[bytes] = mapped_column(LargeBinary)
    metadata_type: Mapped[str] = mapped_column(Text)
    checkpoint_metadata: Mapped[bytes] = mapped_column(LargeBinary)


class WorkflowCheckpointWrite(UUIDPrimaryKeyMixin, CreatedAtMixin, TenantMixin, Base):
    """Output of a task that finished before its step was checkpointed (and interrupts)."""

    __tablename__ = "workflow_checkpoint_writes"
    __table_args__ = (
        UniqueConstraint("thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx"),
    )

    thread_id: Mapped[str] = mapped_column(Text)
    checkpoint_ns: Mapped[str] = mapped_column(Text, server_default="", default="")
    checkpoint_id: Mapped[str] = mapped_column(Text)
    task_id: Mapped[str] = mapped_column(Text)
    task_path: Mapped[str] = mapped_column(Text, server_default="", default="")
    idx: Mapped[int]
    channel: Mapped[str] = mapped_column(Text)
    value_type: Mapped[str] = mapped_column(Text)
    value: Mapped[bytes] = mapped_column(LargeBinary)
