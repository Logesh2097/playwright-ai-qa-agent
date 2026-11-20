"""What a test report contains. The web page, JSON, CSV and PDF all render this."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

REPORT_VERSION = 1
SUGGESTION_NOTE = (
    "AI-generated suggestion. Not a confirmed root cause, and it does not affect any status."
)


class _Part(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Named(_Part):
    id: uuid.UUID
    name: str


class EnvironmentInfo(Named):
    kind: str
    base_url: str


class InstructionInfo(_Part):
    id: uuid.UUID
    text: str


class ExecutionInfo(_Part):
    id: uuid.UUID
    status: str = Field(description="Lifecycle: completed, failed, cancelled, timed_out…")
    verdict: str | None = Field(description="Final outcome, from the results. Null while running.")
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    triggered_by: str | None
    browser: str | None
    config: dict[str, Any]
    error: dict[str, Any] | None


class Totals(_Part):
    total: int
    passed: int
    failed: int
    skipped: int
    blocked: int
    error: int
    flaky: int
    needs_review: int
    # Of the cases that ran to an end. Null when none did.
    pass_rate: float | None


class EvidenceRef(_Part):
    id: uuid.UUID
    kind: str
    content_type: str
    size_bytes: int
    step_position: int | None
    expires_at: datetime | None


class StepDetail(_Part):
    attempt: int
    position: int
    phase: str
    action: str
    description: str | None
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    locator: dict[str, Any] | None
    healed_from: dict[str, Any] | None
    expected: dict[str, Any] | None = Field(description="Assertion: what the case expects.")
    actual: dict[str, Any] | None = Field(description="Assertion: what was found.")
    error_type: str | None
    error_message: str | None
    page_url: str | None


class ResultDetail(_Part):
    id: uuid.UUID
    case_id: uuid.UUID
    case_key: str
    title: str
    version: int
    expected_result: str | None
    status: str
    failure_category: str | None = Field(description="Decided by rules, never by a model.")
    failure_summary: str | None
    healed: bool
    attempts: int
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    console_errors: list[str]
    page_errors: list[str]
    network_errors: list[dict[str, Any]]
    verification: dict[str, Any] | None
    steps: list[StepDetail]
    evidence: list[EvidenceRef]
    ai_suggestion: dict[str, Any] | None = Field(description=SUGGESTION_NOTE)


class FailureSummary(_Part):
    result_id: uuid.UUID
    case_key: str
    title: str
    status: str
    failure_category: str | None
    summary: str | None
    failed_step: str | None
    expected: dict[str, Any] | None
    actual: dict[str, Any] | None
    screenshot_ids: list[uuid.UUID]


class ExecutionReport(_Part):
    report_version: int = REPORT_VERSION
    generated_at: datetime
    project: Named
    application: Named | None
    environment: EnvironmentInfo | None
    instruction: InstructionInfo | None
    suite: Named | None
    execution: ExecutionInfo
    totals: Totals
    failure_categories: dict[str, int]
    failures: list[FailureSummary]
    results: list[ResultDetail]
    notes: list[str] = Field(description="What a reader should know about this report.")
