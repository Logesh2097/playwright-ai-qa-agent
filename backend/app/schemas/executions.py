"""Test execution: scripts, test data, executions, results, steps and evidence."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from app.engine.protocol import RunConfig
from app.schemas.common import ApiModel
from app.schemas.test_cases import CaseValidation
from app.testing.automation import Script

DataName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")]


class AutomationResponse(ApiModel):
    case_id: uuid.UUID
    key: str
    version: int
    status: str = Field(description="A changed script needs the case to be approved again.")
    script: Script | None
    validation: CaseValidation


class DataSetWrite(ApiModel):
    name: DataName
    environment_id: uuid.UUID | None = Field(
        default=None, description="Give one to override the general set of the same name there."
    )
    description: Annotated[str, StringConstraints(max_length=1000)] | None = None
    values: dict[DataName, Annotated[str, StringConstraints(max_length=2000)]] = Field(
        max_length=100
    )
    is_synthetic: bool = Field(description="Must be true: confirms this is made-up test data.")


class DataSetResponse(ApiModel):
    id: uuid.UUID
    name: str
    environment_id: uuid.UUID | None
    description: str | None
    values: dict[str, str]
    created_at: datetime
    updated_at: datetime


class TestFileCreate(ApiModel):
    __test__ = False
    name: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")]
    content_type: str
    content_base64: str = Field(max_length=4_000_000)
    is_synthetic: bool = Field(
        description="Must be true: confirms the file holds no real customer data."
    )


class TestFileResponse(ApiModel):
    __test__ = False
    id: uuid.UUID
    name: str
    content_type: str
    size_bytes: int
    sha256: str
    created_at: datetime


class ExecutionCreate(ApiModel):
    environment_id: uuid.UUID
    suite_id: uuid.UUID | None = Field(default=None, description="Run an approved suite.")
    case_ids: list[uuid.UUID] | None = Field(
        default=None, max_length=500, description="Or: run these approved cases."
    )
    config: RunConfig = Field(default_factory=RunConfig)
    approve_sensitive_actions: bool = Field(
        default=False,
        description="The person starting the run approves its sensitive steps (deleting, paying, "
        "sending messages, changing configuration) for this execution. Never possible on production.",
    )

    @model_validator(mode="after")
    def _one_selection(self) -> Self:
        if (self.suite_id is None) == (not self.case_ids):
            raise ValueError("give either suite_id or case_ids")
        return self


class ResultResponse(ApiModel):
    id: uuid.UUID
    test_case_id: uuid.UUID
    case_key: str
    title: str
    version: int
    status: str
    attempt_count: int
    failure_class: str | None
    failure_category: str | None = Field(
        default=None,
        description="What kind of problem a result that did not pass points at. Decided by rules.",
    )
    failure_summary: str | None
    healed: bool = Field(
        default=False, description="A locator was replaced during the run; needs review."
    )
    has_ai_suggestion: bool = False
    browser: str
    viewport: str
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    details: dict[str, Any]


class ExecutionSummary(ApiModel):
    id: uuid.UUID
    environment_id: uuid.UUID
    suite_id: uuid.UUID | None
    status: str
    verdict: str | None = Field(description="Set when the execution has ended.")
    totals: dict[str, Any]
    triggered_by: uuid.UUID | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ExecutionResponse(ExecutionSummary):
    config: dict[str, Any]
    error: dict[str, Any] | None
    results: list[ResultResponse]


class StepResponse(ApiModel):
    id: uuid.UUID
    attempt: int
    position: int
    phase: Literal["setup", "main", "cleanup"]
    action: str
    description: str | None
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    script_index: int | None = None
    locator: dict[str, Any] | None
    healed_from: dict[str, Any] | None = Field(
        default=None,
        description="The script's own locator, when the engine used a replacement for it.",
    )
    expected: dict[str, Any] | None
    actual: dict[str, Any] | None
    error_type: str | None
    error_message: str | None
    page_url: str | None
    evidence_ids: list[uuid.UUID]


class EvidenceResponse(ApiModel):
    id: uuid.UUID
    result_id: uuid.UUID | None
    step_id: uuid.UUID | None
    kind: str
    content_type: str
    size_bytes: int
    sha256: str
    captured_at: datetime | None
    expires_at: datetime | None
    redaction_status: str = "not_required"


# --- failure reports, replacement locators, stability ------------------------------------


class FailureReportResponse(ApiModel):
    record: dict[str, Any] = Field(
        description="What was recorded: the failed step, expected and found values, browser, "
        "console and network errors, evidence, and the category the rules assigned."
    )
    ai_suggestion: dict[str, Any] | None = Field(
        description="A model's summary, possible root cause, impact and next steps. "
        "A suggestion only: not a confirmed root cause, and it changes no result."
    )


class HealProposalResponse(ApiModel):
    id: uuid.UUID
    test_case_id: uuid.UUID | None
    test_case_version_id: uuid.UUID | None
    result_id: uuid.UUID | None
    script_phase: str
    script_index: int | None
    action: str | None
    old_locator: dict[str, Any]
    new_locator: dict[str, Any]
    confidence: float | None
    rationale: str | None
    held_back: str | None = Field(description="Why the engine did not use it by itself.")
    status: Literal["proposed", "verified", "accepted", "rejected"]
    verified_by_rerun: bool = Field(
        description="The engine used the replacement in that run and the step then passed."
    )
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    created_at: datetime


class HealDecisionResponse(ApiModel):
    proposal: HealProposalResponse
    case_status: str | None = Field(
        default=None, description="After accepting: the case needs approval again."
    )


class StabilityResponse(ApiModel):
    case_id: uuid.UUID
    case_key: str
    title: str
    runs: int
    passed: int
    not_passed: int
    passed_on_retry: int
    flips: int
    flake_rate: float
    is_flaky: bool
    kinds: list[str]
    evidence: list[str]
    last_status: str | None


class EvidenceDownloadUrl(ApiModel):
    url: str = Field(description="Works for this user only, until `expires_at`. Not a share link.")
    expires_at: datetime
