"""The agent workflow: starting a run, answering its questions, deciding its approvals."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from app.engine.protocol import RunConfig
from app.schemas.common import ApiModel
from app.schemas.instructions import (
    ClarificationAnswer,
    InstructionOptions,
    InstructionText,
    TestType,
)


class WorkflowOptions(ApiModel):
    max_retries: int = Field(
        default=1,
        ge=0,
        le=2,
        description="Further attempts for cases that failed for transient reasons.",
    )
    max_regenerations: int = Field(
        default=1, ge=0, le=2, description="Times scripts that fail the checks are written again."
    )
    analyse_failures: bool = Field(
        default=True, description="Ask the AI model to explain failures. Advice only."
    )
    discover_application: bool = Field(
        default=True, description="Look at the application read-only before writing scripts."
    )
    run_config: RunConfig = Field(default_factory=RunConfig)


class WorkflowCreate(ApiModel):
    environment_id: uuid.UUID
    application_id: uuid.UUID | None = Field(default=None, description="Required with `text`.")
    text: InstructionText | None = Field(
        default=None, description="What to test, in plain language. Starts from the instruction."
    )
    test_types: list[TestType] = Field(default=["ui"], min_length=1, max_length=4)
    rules: InstructionOptions = InstructionOptions()
    suite_id: uuid.UUID | None = Field(
        default=None, description="Instead of `text`: run an approved suite."
    )
    options: WorkflowOptions = Field(default_factory=WorkflowOptions)

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        if (self.text is None) == (self.suite_id is None):
            raise ValueError("give either text or suite_id")
        if self.text is not None and self.application_id is None:
            raise ValueError("application_id is required with text")
        return self


class ClarificationSubmit(ApiModel):
    answers: list[ClarificationAnswer] = Field(min_length=1, max_length=20)


class ApprovalDecision(ApiModel):
    decision: Literal["approve", "reject"]
    note: Annotated[str, StringConstraints(max_length=2000)] | None = None


class NodeEvent(ApiModel):
    node: str | None
    event: str
    status: str | None
    duration_ms: int | None
    at: datetime | None
    error: dict[str, Any] | None


class WorkflowSummary(ApiModel):
    id: uuid.UUID
    mode: str
    status: str
    verdict: str | None
    current_node: str | None
    environment_id: uuid.UUID
    instruction_id: uuid.UUID | None
    suite_id: uuid.UUID | None
    execution_ids: list[uuid.UUID]
    retry_count: int
    started_by: uuid.UUID | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class WorkflowResponse(WorkflowSummary):
    options: dict[str, Any]
    waiting_for: dict[str, Any] | None = Field(
        description="Questions to answer, or what needs approval and why."
    )
    error: dict[str, Any] | None
    usage: dict[str, Any]
    history: list[NodeEvent]
    has_report: bool
