"""API keys and the CI interface."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from app.engine.protocol import RunConfig
from app.schemas.common import ApiModel

Scope = Literal["executions:write", "executions:read"]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Sha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{7,64}$")]


class ApiKeyCreate(ApiModel):
    name: Name
    scopes: list[Scope] = Field(min_length=1, max_length=2)
    project_id: uuid.UUID | None = Field(default=None, description="Limit the key to one project.")
    expires_in_days: int | None = Field(default=90, ge=1, le=366)


class ApiKeyResponse(ApiModel):
    id: uuid.UUID
    name: str
    key_prefix: str = Field(description="The beginning of the key, to recognise it by.")
    scopes: list[str]
    project_id: uuid.UUID | None
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None


class ApiKeyCreated(ApiKeyResponse):
    key: str = Field(description="The key itself. Shown this once and never again.")


class GitInfo(ApiModel):
    """What the run is for. Recorded with the execution; nothing is fetched from it."""

    repository: Annotated[str, StringConstraints(max_length=200)] | None = None
    sha: Sha | None = None
    ref: Annotated[str, StringConstraints(max_length=300)] | None = None
    pull_request: int | None = Field(default=None, ge=1)
    run_url: Annotated[str, StringConstraints(max_length=500, pattern=r"^https://")] | None = None


class CiExecutionCreate(ApiModel):
    project_id: uuid.UUID
    environment_id: uuid.UUID
    suite_id: uuid.UUID | None = None
    case_ids: list[uuid.UUID] | None = Field(default=None, max_length=500)
    config: RunConfig = Field(default_factory=RunConfig)
    git: GitInfo | None = None

    @model_validator(mode="after")
    def _one_selection(self) -> Self:
        if (self.suite_id is None) == (not self.case_ids):
            raise ValueError("give either suite_id or case_ids")
        return self


class CiFailure(ApiModel):
    case_key: str
    title: str
    status: str
    failure_category: str | None
    summary: str | None


class CiExecutionResponse(ApiModel):
    id: uuid.UUID
    project_id: uuid.UUID
    status: str
    verdict: str | None
    finished: bool
    outcome: Literal["running", "passed", "failed", "unverified", "incomplete"]
    passed: bool = Field(description="True only for a verified pass of every test.")
    exit_code: int | None = Field(description="0 passed, 1 failed, 2 unverified, 3 incomplete.")
    summary: str
    totals: dict[str, Any]
    failures: list[CiFailure]
    report_url: str = Field(description="The report in the web application. Requires signing in.")
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
