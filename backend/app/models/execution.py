"""Executions, results, evidence, reports, approvals and heal proposals."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    EMPTY_JSON_OBJECT,
    Base,
    CreatedAtMixin,
    TenantMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    one_of,
)

EXECUTION_TRIGGERS = ("manual", "api", "ci", "github_webhook")
EXECUTION_STATUSES = (
    "queued",
    "awaiting_approval",
    "preparing",
    "running",
    "analysing",
    "reporting",
    "completed",
    "failed",
    "cancelling",
    "cancelled",
    "timed_out",
)
ACTIVE_EXECUTION_STATUSES = ("queued", "preparing", "running", "analysing")
# needs_review: nothing failed, but a person has to look at something before the run
# can be called passed (for example a test that only passed with a replacement locator).
EXECUTION_VERDICTS = ("passed", "failed", "flaky", "error", "inconclusive", "needs_review")
RESULT_STATUSES = (
    "pending",
    "running",
    "passed",
    "failed",
    "flaky",
    "error",
    "skipped",
    "blocked",
    "inconclusive",
    "needs_review",
)
# What kind of problem a result that did not pass points at. Decided by code from the
# recorded facts (`app.testing.classification`), never by a model.
FAILURE_CATEGORIES = (
    "application_failure",
    "test_assertion_failure",
    "test_automation_failure",
    "browser_infrastructure_failure",
    "network_failure",
    "authentication_failure",
    "inconclusive",
)
FAILURE_CLASSES = (
    "assertion",
    "locator",
    "timeout",
    "navigation",
    "network",
    "app_error",
    "test_data",
    "environment",
    "infrastructure",
    "unknown",
)
STEP_STATUSES = ("passed", "failed", "error", "skipped")
# setup: signing in before the case. cleanup: undoing what the case created.
STEP_PHASES = ("setup", "main", "cleanup")
FINDING_KINDS = (
    "console_error",
    "failed_request",
    "dead_control",
    "validation_gap",
    "layout_overflow",
    "js_exception",
)
FINDING_SEVERITIES = ("info", "low", "medium", "high")
FINDING_DETECTIONS = ("deterministic", "heuristic", "ai")
EVIDENCE_KINDS = (
    "screenshot",
    "video",
    "trace",
    "console_log",
    "network_log",
    "dom_snapshot",
    "api_exchange",
)
REDACTION_STATUSES = ("not_required", "applied", "failed_quarantined")
REPORT_FORMATS = ("json", "html", "junit")
REPORT_STATUSES = ("pending", "ready", "failed")
HEAL_STATUSES = ("proposed", "verified", "accepted", "rejected")
APPROVAL_SUBJECTS = ("test_plan", "test_cases", "execution", "heal_proposal")
APPROVAL_REASONS = ("production_environment", "mutating_steps", "llm_inferred_oracle", "policy")
APPROVAL_STATUSES = ("pending", "approved", "rejected", "expired")


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class TestExecution(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    """One run. `status` is the lifecycle; `verdict` is the outcome."""

    __tablename__ = "test_executions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "organization_id"],
            ["projects.id", "projects.organization_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["environment_id", "organization_id"],
            ["environments.id", "environments.organization_id"],
            ondelete="RESTRICT",
        ),
        Index("ix_test_executions_project_created", "project_id", "created_at"),
        Index("ix_test_executions_environment_created", "environment_id", "created_at"),
        Index(
            "ix_test_executions_org_active",
            "organization_id",
            "status",
            postgresql_where=text(f"status IN ({_quoted(ACTIVE_EXECUTION_STATUSES)})"),
        ),
        Index(
            "uq_test_executions_org_idempotency_key",
            "organization_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        one_of("trigger", EXECUTION_TRIGGERS),
        one_of("status", EXECUTION_STATUSES),
        one_of("verdict", EXECUTION_VERDICTS),
    )

    project_id: Mapped[uuid.UUID]
    environment_id: Mapped[uuid.UUID]
    suite_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("test_suites.id", ondelete="SET NULL")
    )
    instruction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("test_instructions.id", ondelete="SET NULL")
    )
    trigger: Mapped[str] = mapped_column(Text, server_default="manual", default="manual")
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("api_keys.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(Text, server_default="queued", default="queued")
    verdict: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON_OBJECT, default=dict)
    git_context: Mapped[dict[str, Any] | None]
    totals: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON_OBJECT, default=dict)
    runner_id: Mapped[str | None] = mapped_column(Text)
    mcp_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("mcp_connections.id", ondelete="SET NULL")
    )
    graph_thread_id: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    timeout_s: Mapped[int | None]
    llm_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), server_default=text("0"), default=Decimal("0")
    )
    queued_at: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    cancel_requested_at: Mapped[datetime | None]
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    error: Mapped[dict[str, Any] | None]


class TestResult(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    """Outcome of one case x dataset row x viewport x browser."""

    __tablename__ = "test_results"
    __table_args__ = (
        UniqueConstraint("execution_id", "test_case_id", "dataset_index", "viewport", "browser"),
        Index("ix_test_results_case_finished", "test_case_id", "finished_at"),
        Index("ix_test_results_execution_status", "execution_id", "status"),
        one_of("status", RESULT_STATUSES),
        one_of("failure_class", FAILURE_CLASSES),
        one_of("failure_category", FAILURE_CATEGORIES),
    )

    execution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("test_executions.id", ondelete="CASCADE")
    )
    test_case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("test_cases.id", ondelete="RESTRICT")
    )
    test_case_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("test_case_versions.id", ondelete="RESTRICT")
    )
    dataset_index: Mapped[int] = mapped_column(server_default=text("0"), default=0)
    viewport: Mapped[str] = mapped_column(Text, server_default="desktop", default="desktop")
    browser: Mapped[str] = mapped_column(Text, server_default="chromium", default="chromium")
    status: Mapped[str] = mapped_column(Text, server_default="pending", default="pending")
    attempt_count: Mapped[int] = mapped_column(server_default=text("0"), default=0)
    failure_class: Mapped[str | None] = mapped_column(Text)
    failure_category: Mapped[str | None] = mapped_column(Text)
    failure_summary: Mapped[str | None] = mapped_column(Text)
    # Advisory only. Verdict logic must never read this column.
    ai_analysis: Mapped[dict[str, Any] | None]
    healed: Mapped[bool] = mapped_column(server_default=text("false"), default=False)
    duration_ms: Mapped[int | None]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    # What the browser reported while the case ran (console errors, failed and blocked
    # requests), whether cleanup worked, and notes about evidence. Not a verdict.
    details: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON_OBJECT, default=dict)


class TestExecutionStep(UUIDPrimaryKeyMixin, CreatedAtMixin, TenantMixin, Base):
    __tablename__ = "test_execution_steps"
    __table_args__ = (
        UniqueConstraint("result_id", "attempt", "position"),
        one_of("status", STEP_STATUSES),
        one_of("phase", STEP_PHASES),
    )

    execution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("test_executions.id", ondelete="CASCADE"), index=True
    )
    result_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("test_results.id", ondelete="CASCADE"))
    test_step_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("test_steps.id", ondelete="SET NULL")
    )
    attempt: Mapped[int] = mapped_column(server_default=text("1"), default=1)
    position: Mapped[int]
    # Which step of the script this was (0-based, within its phase). Null for sign-in.
    script_index: Mapped[int | None]
    phase: Mapped[str] = mapped_column(Text, server_default="main", default="main")
    action: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    page_url: Mapped[str | None] = mapped_column(Text)
    resolved_locator: Mapped[dict[str, Any] | None]
    expected: Mapped[dict[str, Any] | None]
    actual: Mapped[dict[str, Any] | None]
    error_type: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    healed_from: Mapped[dict[str, Any] | None]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    duration_ms: Mapped[int | None]


class TestEvidence(UUIDPrimaryKeyMixin, CreatedAtMixin, TenantMixin, Base):
    __tablename__ = "test_evidence"
    __table_args__ = (
        one_of("kind", EVIDENCE_KINDS),
        one_of("redaction_status", REDACTION_STATUSES),
    )

    execution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("test_executions.id", ondelete="CASCADE"), index=True
    )
    result_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("test_results.id", ondelete="CASCADE"), index=True
    )
    execution_step_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("test_execution_steps.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(Text)
    storage_key: Mapped[str] = mapped_column(Text, unique=True)
    content_type: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(Text)
    redaction_status: Mapped[str] = mapped_column(
        Text, server_default="not_required", default="not_required"
    )
    viewport: Mapped[str | None] = mapped_column(Text)
    captured_at: Mapped[datetime | None]
    expires_at: Mapped[datetime | None] = mapped_column(index=True)


class TestFinding(UUIDPrimaryKeyMixin, CreatedAtMixin, TenantMixin, Base):
    """Audit-mode observation. A finding is not a pass/fail verdict."""

    __tablename__ = "test_findings"
    __table_args__ = (
        Index("ix_test_findings_execution_kind", "execution_id", "kind"),
        one_of("kind", FINDING_KINDS),
        one_of("severity", FINDING_SEVERITIES),
        one_of("detection", FINDING_DETECTIONS),
    )

    execution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("test_executions.id", ondelete="CASCADE")
    )
    result_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("test_results.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text, server_default="info", default="info")
    detection: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON_OBJECT, default=dict)
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("test_evidence.id", ondelete="SET NULL")
    )


class TestReport(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "test_reports"
    __table_args__ = (
        UniqueConstraint("execution_id", "format"),
        one_of("format", REPORT_FORMATS),
        one_of("status", REPORT_STATUSES),
    )

    execution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("test_executions.id", ondelete="CASCADE")
    )
    format: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="pending", default="pending")
    summary: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON_OBJECT, default=dict)
    storage_key: Mapped[str | None] = mapped_column(Text)
    generated_at: Mapped[datetime | None]
    share_token_hash: Mapped[str | None] = mapped_column(Text, unique=True)
    share_expires_at: Mapped[datetime | None]


class LocatorHealProposal(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    """A replacement for a locator that no longer matched, found by the engine.

    A proposal never changes a test case by itself. `verified` means the engine
    used it in that run and the step then passed; a person still has to accept
    it, which creates a new case version that needs approval like any other.
    """

    __tablename__ = "locator_heal_proposals"
    __table_args__ = (
        Index("ix_locator_heal_proposals_org_status", "organization_id", "status"),
        Index("ix_locator_heal_proposals_case", "test_case_id", "created_at"),
        one_of("status", HEAL_STATUSES),
        one_of("script_phase", STEP_PHASES),
    )

    # Unused: steps come from the case version's script (see `script_index`).
    test_step_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("test_steps.id", ondelete="CASCADE")
    )
    test_case_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("test_cases.id", ondelete="CASCADE")
    )
    test_case_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("test_case_versions.id", ondelete="CASCADE")
    )
    result_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("test_results.id", ondelete="SET NULL")
    )
    execution_step_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("test_execution_steps.id", ondelete="SET NULL")
    )
    script_phase: Mapped[str] = mapped_column(Text, server_default="main", default="main")
    script_index: Mapped[int | None]
    action: Mapped[str | None] = mapped_column(Text)
    old_locator: Mapped[dict[str, Any]]
    new_locator: Mapped[dict[str, Any]]
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    rationale: Mapped[str | None] = mapped_column(Text)
    # Why the engine did not use it by itself, if it did not.
    held_back: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="proposed", default="proposed")
    verified_by_rerun: Mapped[bool] = mapped_column(server_default=text("false"), default=False)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    decided_at: Mapped[datetime | None]


class Approval(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    """A human decision the workflow is waiting for."""

    __tablename__ = "approvals"
    __table_args__ = (
        Index("ix_approvals_org_status", "organization_id", "status"),
        Index("ix_approvals_subject", "subject_type", "subject_id"),
        one_of("subject_type", APPROVAL_SUBJECTS),
        one_of("reason_code", APPROVAL_REASONS),
        one_of("status", APPROVAL_STATUSES),
    )

    subject_type: Mapped[str] = mapped_column(Text)
    subject_id: Mapped[uuid.UUID]
    reason_code: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON_OBJECT, default=dict)
    status: Mapped[str] = mapped_column(Text, server_default="pending", default="pending")
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    decided_at: Mapped[datetime | None]
    decision_note: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None]
