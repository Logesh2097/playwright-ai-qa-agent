"""Request and response bodies for test cases and suites."""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from app.schemas.common import ApiModel
from app.testing.cases import CaseContent, Category, Priority, Severity

CaseStatus = Literal["draft", "needs_review", "approved", "deprecated"]
SuiteStatus = Literal["draft", "approved", "archived"]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]
Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]


class CaseIssueResponse(ApiModel):
    code: str
    severity: Literal["blocking", "warning"]
    message: str
    step: int | None = None


class CaseValidation(ApiModel):
    ready: bool = Field(description="True when nothing blocks approval.")
    issues: list[CaseIssueResponse]


class CaseCreate(ApiModel):
    """A test case written by a person."""

    application_id: uuid.UUID
    environment_id: uuid.UUID | None = None
    instruction_id: uuid.UUID | None = Field(
        default=None, description="Attach the case to an instruction's set of cases."
    )
    content: CaseContent


class CaseSummary(ApiModel):
    id: uuid.UUID
    key: str = Field(description="Test case ID shown to people, e.g. TC-0042.")
    title: str
    category: Category
    test_type: str
    priority: Priority
    severity: Severity
    tags: list[str]
    status: CaseStatus
    source: Literal["ai_generated", "manual"]
    instruction_id: uuid.UUID | None
    objective_id: str | None
    version: int
    version_source: Literal["ai", "human"]
    has_expected_result: bool
    ready: bool
    blocking_issues: int
    updated_at: datetime


class CaseResponse(CaseSummary):
    project_id: uuid.UUID
    application_id: uuid.UUID
    environment_id: uuid.UUID | None
    content: CaseContent
    validation: CaseValidation
    # Version numbers. `original_version` is what the AI wrote, untouched.
    original_version: int | None
    approved_version: int | None
    created_at: datetime


class CaseVersionSummary(ApiModel):
    version: int
    source: Literal["ai", "human"]
    change_reason: Literal["created", "edited", "healed", "regenerated"]
    title: str
    created_by: uuid.UUID | None
    created_at: datetime
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    is_current: bool
    is_original: bool
    is_approved: bool


class CaseVersionResponse(CaseVersionSummary):
    content: CaseContent
    validation: CaseValidation | None


class RegenerateRequest(ApiModel):
    case_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)
    feedback: Text | None = Field(default=None, description="What is wrong with these cases.")


class SuiteCreate(ApiModel):
    name: Name
    application_id: uuid.UUID
    description: Text | None = None
    feature: Name | None = Field(default=None, description="Suites are grouped by feature.")
    priority: Priority = Field(default="p2", description="Which suites run first.")
    tags: list[Tag] = Field(default_factory=list, max_length=20)
    case_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)


class SuiteUpdate(ApiModel):
    name: Name | None = None
    description: Text | None = None
    feature: Name | None = None
    priority: Priority | None = None
    tags: list[Tag] | None = Field(default=None, max_length=20)
    archived: bool | None = None


class SuiteCases(ApiModel):
    case_ids: list[uuid.UUID] = Field(
        max_length=500, description="All cases of the suite, in execution order."
    )


class SuiteCaseResponse(ApiModel):
    position: int
    id: uuid.UUID
    key: str
    title: str
    category: Category
    priority: Priority
    status: CaseStatus
    current_version: int | None
    approved_version: int | None = Field(description="The version approved with the suite.")
    outdated: bool = Field(description="The case changed after the suite was approved.")


class SuiteResponse(ApiModel):
    id: uuid.UUID
    project_id: uuid.UUID
    application_id: uuid.UUID
    instruction_id: uuid.UUID | None
    name: str
    description: str | None
    feature: str | None
    priority: Priority
    tags: list[str]
    status: SuiteStatus
    source: Literal["ai_generated", "manual"]
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    cases: list[SuiteCaseResponse]
    created_at: datetime
    updated_at: datetime
