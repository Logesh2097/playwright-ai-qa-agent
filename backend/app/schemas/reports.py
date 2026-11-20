"""Execution history, comparison, trends and dashboard."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from app.schemas.common import ApiModel
from app.schemas.executions import ExecutionSummary


class ExecutionPageResponse(ApiModel):
    items: list[ExecutionSummary]
    total: int
    limit: int
    offset: int


class HistoryEntryResponse(ApiModel):
    result_id: uuid.UUID
    execution_id: uuid.UUID
    environment_id: uuid.UUID
    case_id: uuid.UUID
    case_key: str | None
    title: str | None
    status: str
    failure_category: str | None
    failure_summary: str | None
    healed: bool
    attempts: int
    duration_ms: int | None
    finished_at: datetime | None


class ResultRates(ApiModel):
    ran: int = Field(description="Cases that ran to an end: the base of both rates.")
    passed: int
    failed: int
    error: int
    flaky: int
    needs_review: int
    skipped: int
    blocked: int
    pass_rate: float | None
    failure_rate: float | None


class ComparedExecution(ApiModel):
    id: uuid.UUID
    status: str
    verdict: str | None
    environment_id: uuid.UUID
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    results: ResultRates


class ComparedCase(ApiModel):
    case_id: uuid.UUID
    case_key: str | None
    title: str | None
    base_status: str | None
    target_status: str | None
    base_category: str | None
    target_category: str | None
    base_duration_ms: int | None
    target_duration_ms: int | None
    change: str = Field(
        description="regressed, fixed, still_failing, changed, added, removed or unchanged"
    )


class ComparisonResponse(ApiModel):
    base: ComparedExecution
    target: ComparedExecution
    same_environment: bool
    changes: dict[str, int]
    cases: list[ComparedCase]


class TrendPoint(ApiModel):
    date: str
    executions: int
    verdicts: dict[str, int]
    results: ResultRates
    average_duration_ms: int | None


class TrendResponse(ApiModel):
    window_days: int
    truncated: bool = Field(description="The window holds more rows than are read at once.")
    points: list[TrendPoint]


class FailingCase(ApiModel):
    case_id: uuid.UUID
    case_key: str | None
    title: str | None
    runs: int
    failures: int
    failure_rate: float | None
    last_status: str
    last_failed_at: datetime | None
    most_common_category: str | None


class CoverageIndicators(ApiModel):
    """About the project's test cases. Not code coverage."""

    test_cases: int
    approved: int
    automated: int = Field(description="Approved cases with an executable script.")
    executed: int = Field(description="Automated cases that ran to an end in the window.")
    passing: int = Field(description="Automated cases whose latest result in the window passed.")
    approved_share: float | None
    automated_share: float | None
    executed_share: float | None
    passing_share: float | None
    note: str


class DashboardResponse(ApiModel):
    window_days: int
    truncated: bool
    total_executions: int
    finished_executions: int
    executions_by_verdict: dict[str, int]
    execution_pass_rate: float | None = Field(description="Executions with verdict `passed`.")
    execution_failure_rate: float | None = Field(description="Verdict `failed` or `error`.")
    results: ResultRates
    flaky_test_count: int
    flaky_case_ids: list[uuid.UUID]
    average_execution_duration_ms: int | None
    frequently_failing: list[FailingCase]
    coverage: CoverageIndicators
    recent_executions: list[ExecutionSummary]


class OrgFailingCase(FailingCase):
    project_id: uuid.UUID | None
    project_name: str | None


class OrgRecentExecution(ExecutionSummary):
    project_id: uuid.UUID
    project_name: str


class OrganizationDashboardResponse(ApiModel):
    """Across the projects the caller can see."""

    window_days: int
    truncated: bool
    total_projects: int
    active_projects: int
    total_executions: int
    finished_executions: int
    results: ResultRates
    points: list[TrendPoint]
    frequently_failing: list[OrgFailingCase]
    recent_executions: list[OrgRecentExecution]


class LiveResult(ApiModel):
    id: uuid.UUID
    case_key: str
    title: str
    status: str
    attempts: int
    failure_summary: str | None


class LiveState(ApiModel):
    """What is known about a running execution right now. Nothing in it is estimated."""

    id: uuid.UUID
    status: str
    verdict: str | None
    finished: bool
    totals: dict[str, int]
    started_at: datetime | None
    finished_at: datetime | None
    results: list[LiveResult]
    # The case the engine is working on and the last step it reported, if it is running.
    current: dict[str, Any] | None
