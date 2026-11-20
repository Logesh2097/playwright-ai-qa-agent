"""Request and response bodies for test instructions and requirement analysis."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints

from app.agent.schemas.requirement import ClarificationQuestion, RequirementAnalysis
from app.schemas.common import ApiModel

InstructionStatus = Literal[
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
]
TestType = Literal["ui", "api", "responsive", "audit"]

InstructionText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=10)]
Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Long = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class ConfiguredRule(ApiModel):
    """A business rule or value the tester supplies up front, e.g. the minimum bill amount."""

    name: Short
    value: Long
    description: Long | None = None


class InstructionOptions(ApiModel):
    rules: list[ConfiguredRule] = Field(default_factory=list, max_length=30)


class InstructionCreate(ApiModel):
    application_id: uuid.UUID
    environment_id: uuid.UUID
    text: InstructionText = Field(description="What to test, in plain language.")
    test_types: list[TestType] = Field(default=["ui"], min_length=1, max_length=4)
    options: InstructionOptions = InstructionOptions()


class InstructionUpdate(ApiModel):
    """Changing the text or options discards the analysis and starts a new one."""

    text: InstructionText | None = None
    test_types: list[TestType] | None = Field(default=None, min_length=1, max_length=4)
    options: InstructionOptions | None = None


class ClarificationAnswer(ApiModel):
    question_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    answer: Long


class ClarificationSubmit(ApiModel):
    answers: list[ClarificationAnswer] = Field(min_length=1, max_length=20)


class AnsweredQuestion(ApiModel):
    question_id: str
    question: str
    answer: str
    answered_by: uuid.UUID | None
    answered_at: datetime


class ValidationIssue(ApiModel):
    code: str
    severity: Literal["blocking", "warning"]
    message: str
    subject: str | None = None


class ValidationResult(ApiModel):
    ready: bool = Field(description="True when nothing blocks approval and nothing is left to ask.")
    issues: list[ValidationIssue]


class InstructionError(ApiModel):
    code: str
    message: str


class UsageSummary(ApiModel):
    llm_calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal | None = Field(description="Null when no token price is configured.")


class InstructionResponse(ApiModel):
    id: uuid.UUID
    project_id: uuid.UUID
    application_id: uuid.UUID
    environment_id: uuid.UUID
    created_by: uuid.UUID | None
    text: str
    test_types: list[TestType]
    options: InstructionOptions
    status: InstructionStatus
    analysis: RequirementAnalysis | None
    analysis_source: Literal["ai", "human_edited"] | None
    analysis_model: str | None
    analysis_prompt: str | None
    analysed_at: datetime | None
    validation: ValidationResult | None
    clarification_questions: list[ClarificationQuestion]
    clarification_answers: list[AnsweredQuestion]
    error: InstructionError | None
    generation: dict[str, Any] | None = Field(
        description="Outcome of the last test-case generation: counts, and an error if it failed."
    )
    usage: UsageSummary
    created_at: datetime
    updated_at: datetime


class PlanResponse(ApiModel):
    id: uuid.UUID
    instruction_id: uuid.UUID
    version: int
    status: Literal["draft", "validated", "awaiting_approval", "approved", "rejected", "superseded"]
    summary: str | None
    scope: dict[str, object]
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    created_at: datetime
