"""Instructions, plans, suites, cases and steps."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
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

INSTRUCTION_STATUSES = (
    "received",
    "analysing",
    "needs_clarification",
    "analysed",
    "generating",
    "awaiting_approval",
    "approved",
    "rejected",
    "failed",
    "cancelled",
    "completed",
)
ANALYSIS_SOURCES = ("ai", "human_edited")
PLAN_STATUSES = ("draft", "validated", "awaiting_approval", "approved", "rejected", "superseded")
SUITE_STATUSES = ("draft", "approved", "archived")
CASE_SOURCES = ("ai_generated", "manual")
CASE_TYPES = ("ui", "api", "responsive", "audit")
CASE_CATEGORIES = (
    "positive",
    "negative",
    "boundary",
    "edge_case",
    "form_validation",
    "navigation",
    "authentication",
    "api_validation",
    "responsive",
    "business_logic",
)
CASE_PRIORITIES = ("p0", "p1", "p2", "p3")
CASE_SEVERITIES = ("critical", "major", "minor", "trivial")
CASE_STATUSES = ("draft", "needs_review", "approved", "deprecated")
AUTOMATION_FEASIBILITY = ("automatable", "partially_automatable", "manual_only")
# Where a case's expected result comes from. "unknown" means nobody has supplied it yet.
CASE_EXPECTED_SOURCES = ("plan_objective", "configured_rule", "human_specified", "unknown")
VERSION_SOURCES = ("ai", "human")
ORACLE_STATUSES = ("verified", "llm_inferred", "none")
VERSION_CHANGE_REASONS = ("created", "edited", "healed", "regenerated")
EXPECTED_SOURCES = ("user_specified", "derived_rule", "dom_constraint", "llm_inferred")
SIDE_EFFECTS = ("none", "create", "update", "delete", "external")


def _project_fk() -> ForeignKeyConstraint:
    # A constraint object can be attached to one table only, so build a new one each time.
    return ForeignKeyConstraint(
        ["project_id", "organization_id"],
        ["projects.id", "projects.organization_id"],
        ondelete="RESTRICT",
    )


class TestInstruction(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "test_instructions"
    __table_args__ = (
        _project_fk(),
        Index("ix_test_instructions_project_created", "project_id", "created_at"),
        Index("ix_test_instructions_org_status", "organization_id", "status"),
        one_of("status", INSTRUCTION_STATUSES),
        one_of("analysis_source", ANALYSIS_SOURCES),
    )

    project_id: Mapped[uuid.UUID]
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="RESTRICT")
    )
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="RESTRICT")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    raw_text: Mapped[str] = mapped_column(Text)
    test_types: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=EMPTY_TEXT_ARRAY, default=list
    )
    options: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON_OBJECT, default=dict)
    status: Mapped[str] = mapped_column(Text, server_default="received", default="received")
    analysis: Mapped[dict[str, Any] | None]
    clarification_questions: Mapped[list[Any] | None]
    clarification_answers: Mapped[list[Any] | None]
    graph_thread_id: Mapped[str | None] = mapped_column(Text)
    error: Mapped[dict[str, Any] | None]
    # Result of the deterministic checks on the analysis: issues and readiness.
    validation: Mapped[dict[str, Any] | None]
    # "ai" or "human_edited": whether a person has changed the analysis since the model wrote it.
    analysis_source: Mapped[str | None] = mapped_column(Text)
    analysis_model: Mapped[str | None] = mapped_column(Text)
    analysis_prompt: Mapped[str | None] = mapped_column(Text)
    analysed_at: Mapped[datetime | None]
    # Identifies the background run in progress (analysis or generation). A run whose
    # token no longer matches (the instruction was edited or cancelled meanwhile)
    # discards its result.
    run_token: Mapped[str | None] = mapped_column(Text)
    llm_call_count: Mapped[int] = mapped_column(server_default=text("0"), default=0)
    # Outcome of the last test-case generation: counts, what was dropped and why.
    generation: Mapped[dict[str, Any] | None]


class TestPlan(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "test_plans"
    __table_args__ = (
        UniqueConstraint("instruction_id", "version"),
        one_of("status", PLAN_STATUSES),
    )

    instruction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("test_instructions.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int]
    status: Mapped[str] = mapped_column(Text, server_default="draft", default="draft")
    summary: Mapped[str | None] = mapped_column(Text)
    scope: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON_OBJECT, default=dict)
    out_of_scope: Mapped[list[Any]] = mapped_column(server_default=EMPTY_JSON_ARRAY, default=list)
    strategy: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON_OBJECT, default=dict)
    risks: Mapped[list[Any]] = mapped_column(server_default=EMPTY_JSON_ARRAY, default=list)
    estimated_case_count: Mapped[int | None]
    model: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    approved_at: Mapped[datetime | None]


class TestSuite(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, TenantMixin, Base):
    __tablename__ = "test_suites"
    __table_args__ = (
        _project_fk(),
        Index(
            "uq_test_suites_project_name_active",
            "project_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        one_of("source", CASE_SOURCES),
        one_of("status", SUITE_STATUSES),
        one_of("priority", CASE_PRIORITIES),
    )

    project_id: Mapped[uuid.UUID]
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, server_default="manual", default="manual")
    instruction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("test_instructions.id", ondelete="SET NULL")
    )
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=EMPTY_TEXT_ARRAY, default=list
    )
    # The feature the suite covers; suites are grouped by it.
    feature: Mapped[str | None] = mapped_column(Text)
    # Which suites run first when several are queued.
    priority: Mapped[str] = mapped_column(Text, server_default="p2", default="p2")
    status: Mapped[str] = mapped_column(Text, server_default="draft", default="draft")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    approved_at: Mapped[datetime | None]


class TestCase(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, TenantMixin, Base):
    __tablename__ = "test_cases"
    __table_args__ = (
        _project_fk(),
        UniqueConstraint("project_id", "key"),
        Index("ix_test_cases_project_status", "project_id", "status"),
        Index("ix_test_cases_tags", "tags", postgresql_using="gin"),
        one_of("type", CASE_TYPES),
        one_of("category", CASE_CATEGORIES),
        one_of("priority", CASE_PRIORITIES),
        one_of("severity", CASE_SEVERITIES),
        one_of("status", CASE_STATUSES),
        one_of("source", CASE_SOURCES),
    )

    project_id: Mapped[uuid.UUID]
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="RESTRICT")
    )
    environment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("environments.id", ondelete="SET NULL")
    )
    key: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    type: Mapped[str] = mapped_column(Text)
    # title, type, category, priority, severity and tags mirror the current version,
    # so that lists and filters do not need to join the version table.
    category: Mapped[str] = mapped_column(Text, server_default="positive", default="positive")
    priority: Mapped[str] = mapped_column(Text, server_default="p2", default="p2")
    severity: Mapped[str] = mapped_column(Text, server_default="minor", default="minor")
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=EMPTY_TEXT_ARRAY, default=list
    )
    status: Mapped[str] = mapped_column(Text, server_default="draft", default="draft")
    # The version exactly as the AI wrote it. Never changed; null for manual cases.
    original_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("test_case_versions.id", ondelete="SET NULL", use_alter=True)
    )
    # The version a reviewer approved. Later edits create newer versions and clear this.
    approved_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("test_case_versions.id", ondelete="SET NULL", use_alter=True)
    )
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "test_case_versions.id",
            ondelete="SET NULL",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        )
    )
    source: Mapped[str] = mapped_column(Text, server_default="manual", default="manual")
    instruction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("test_instructions.id", ondelete="SET NULL")
    )
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("test_plans.id", ondelete="SET NULL")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class TestCaseVersion(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    """Immutable snapshot of a case's steps and data. Executions reference a version."""

    __tablename__ = "test_case_versions"
    __table_args__ = (
        UniqueConstraint("test_case_id", "version"),
        one_of("oracle_status", ORACLE_STATUSES),
        one_of("change_reason", VERSION_CHANGE_REASONS),
        one_of("source", VERSION_SOURCES),
        one_of("category", CASE_CATEGORIES),
        one_of("priority", CASE_PRIORITIES),
        one_of("severity", CASE_SEVERITIES),
        one_of("automation_feasibility", AUTOMATION_FEASIBILITY),
        one_of("expected_source", CASE_EXPECTED_SOURCES),
        # An expected result is either known and written down, or unknown and empty.
        CheckConstraint(
            "(expected_source = 'unknown') = (expected_result IS NULL)",
            name="expected_result_matches_source",
        ),
    )

    test_case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("test_cases.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int]
    # "ai": written by the model. "human": written or edited by a person.
    source: Mapped[str] = mapped_column(Text, server_default="human", default="human")
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    test_type: Mapped[str] = mapped_column(Text, server_default="ui", default="ui")
    # The plan objective this case verifies; null for scenarios beyond the plan.
    objective_id: Mapped[str | None] = mapped_column(Text)
    preconditions: Mapped[list[Any]] = mapped_column(server_default=EMPTY_JSON_ARRAY, default=list)
    test_data: Mapped[list[Any]] = mapped_column(server_default=EMPTY_JSON_ARRAY, default=list)
    expected_result: Mapped[str | None] = mapped_column(Text)
    expected_source: Mapped[str] = mapped_column(Text, server_default="unknown", default="unknown")
    priority: Mapped[str] = mapped_column(Text, server_default="p2", default="p2")
    severity: Mapped[str] = mapped_column(Text, server_default="minor", default="minor")
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=EMPTY_TEXT_ARRAY, default=list
    )
    dependencies: Mapped[list[Any]] = mapped_column(server_default=EMPTY_JSON_ARRAY, default=list)
    automation_feasibility: Mapped[str] = mapped_column(
        Text, server_default="automatable", default="automatable"
    )
    automation_notes: Mapped[str | None] = mapped_column(Text)
    verification: Mapped[dict[str, Any]] = mapped_column(
        server_default=EMPTY_JSON_OBJECT, default=dict
    )
    assumptions: Mapped[list[Any]] = mapped_column(server_default=EMPTY_JSON_ARRAY, default=list)
    # Result of the deterministic checks on this version: issues and readiness.
    validation: Mapped[dict[str, Any] | None]
    # The executable form of the case (`app.testing.automation.Script`). Null: the case
    # is described but cannot be run by the engine yet.
    automation: Mapped[dict[str, Any] | None]
    credential_role: Mapped[str | None] = mapped_column(Text)
    parameters_schema: Mapped[dict[str, Any] | None]
    datasets: Mapped[list[Any]] = mapped_column(server_default=EMPTY_JSON_ARRAY, default=list)
    oracle_status: Mapped[str] = mapped_column(Text, server_default="none", default="none")
    change_reason: Mapped[str] = mapped_column(Text, server_default="created", default="created")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    approved_at: Mapped[datetime | None]


class TestSuiteCase(TenantMixin, Base):
    __tablename__ = "test_suite_cases"
    __table_args__ = (
        UniqueConstraint("suite_id", "position", deferrable=True, initially="DEFERRED"),
    )

    suite_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("test_suites.id", ondelete="CASCADE"), primary_key=True
    )
    test_case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("test_cases.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    # Execution order within the suite.
    position: Mapped[int]
    # Set when the suite is approved: exactly which version of the case was approved.
    approved_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("test_case_versions.id", ondelete="SET NULL")
    )


class TestStep(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "test_steps"
    __table_args__ = (
        UniqueConstraint("test_case_version_id", "position"),
        one_of("expected_source", EXPECTED_SOURCES),
        one_of("side_effect", SIDE_EFFECTS),
        # Every assertion must record where its expected value came from.
        CheckConstraint(
            "NOT is_assertion OR expected_source IS NOT NULL", name="assertion_has_source"
        ),
        CheckConstraint("position >= 1", name="position_positive"),
    )

    test_case_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("test_case_versions.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int]
    action: Mapped[str] = mapped_column(Text)
    target: Mapped[dict[str, Any] | None]
    args: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON_OBJECT, default=dict)
    description: Mapped[str | None] = mapped_column(Text)
    is_assertion: Mapped[bool] = mapped_column(server_default=text("false"), default=False)
    expected: Mapped[dict[str, Any] | None]
    expected_source: Mapped[str | None] = mapped_column(Text)
    side_effect: Mapped[str] = mapped_column(Text, server_default="none", default="none")
    timeout_ms: Mapped[int | None]
    continue_on_failure: Mapped[bool] = mapped_column(server_default=text("false"), default=False)


class TestDataSet(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    """Named, reusable test data of a project: `{{data.NAME}}` in scripts.

    A set without an environment applies everywhere. A set with the same name
    for one environment replaces it there, value by value.
    """

    __tablename__ = "test_data_sets"
    __table_args__ = (
        _project_fk(),
        Index(
            "uq_test_data_sets_project_name_default",
            "project_id",
            "name",
            unique=True,
            postgresql_where=text("environment_id IS NULL"),
        ),
        Index(
            "uq_test_data_sets_project_name_environment",
            "project_id",
            "name",
            "environment_id",
            unique=True,
            postgresql_where=text("environment_id IS NOT NULL"),
        ),
        # Whoever saved it confirmed it holds made-up data, not real customers'.
        CheckConstraint("is_synthetic", name="synthetic_only"),
    )

    project_id: Mapped[uuid.UUID]
    environment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    values: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON_OBJECT, default=dict)
    is_synthetic: Mapped[bool] = mapped_column(server_default=text("true"), default=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class TestFile(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    """A file a test may upload into the application under test. Nothing else can be uploaded."""

    __tablename__ = "test_files"
    __table_args__ = (
        _project_fk(),
        UniqueConstraint("project_id", "name"),
        CheckConstraint("is_synthetic", name="synthetic_only"),
    )

    project_id: Mapped[uuid.UUID]
    name: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(Text)
    content: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    is_synthetic: Mapped[bool] = mapped_column(server_default=text("true"), default=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
