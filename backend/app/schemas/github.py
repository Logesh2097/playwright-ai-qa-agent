"""GitHub connections, linked repositories and what they may trigger."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import Field, SecretStr, StringConstraints, model_validator

from app.schemas.common import ApiModel

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
RepositoryName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        max_length=140,
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$",
    ),
]
# A branch name or a pattern such as `release/*`. No spaces, no control characters.
Branch = Annotated[
    str, StringConstraints(min_length=1, max_length=200, pattern=r"^[^\s~^:\\\x00-\x1f]+$")
]
PathPattern = Annotated[
    str, StringConstraints(min_length=1, max_length=300, pattern=r"^[^\x00-\x1f\\]+$")
]


class ConnectionCreate(ApiModel):
    """Either a fine-grained personal access token, or a GitHub App installation."""

    name: Name
    token: SecretStr | None = Field(default=None, min_length=20, max_length=400)
    app_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9]{1,40}$")] | None = None
    private_key: SecretStr | None = Field(default=None, min_length=200, max_length=8000)
    installation_id: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _one_mechanism(self) -> Self:
        app = (self.app_id, self.private_key, self.installation_id)
        if self.token is not None and any(value is not None for value in app):
            raise ValueError("give either a token or a GitHub App, not both")
        if self.token is None and not all(value is not None for value in app):
            raise ValueError("give a token, or app_id, private_key and installation_id together")
        return self


class ConnectionResponse(ApiModel):
    """A stored connection. Its credentials are never returned."""

    id: uuid.UUID
    name: str
    auth: Literal["token", "app"]
    account: str | None
    status: str
    created_at: datetime


class AvailableRepository(ApiModel):
    id: int
    full_name: str
    default_branch: str
    private: bool
    archived: bool
    html_url: str
    description: str | None
    permissions: dict[str, bool]


class LinkCreate(ApiModel):
    connection_id: uuid.UUID
    full_name: RepositoryName


class PathRule(ApiModel):
    pattern: PathPattern = Field(description="For example `src/billing/**` or `*.md`.")
    suite_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)


class LinkUpdate(ApiModel):
    enabled: bool | None = None
    on_pull_request: bool | None = None
    on_push: bool | None = None
    branches: list[Branch] | None = Field(default=None, max_length=20)
    environment_id: uuid.UUID | None = None
    suite_ids: list[uuid.UUID] | None = Field(default=None, max_length=20)
    selection_mode: Literal["all", "affected"] | None = None
    path_rules: list[PathRule] | None = Field(default=None, max_length=100)
    status_enabled: bool | None = None
    comment_enabled: bool | None = None


class LinkResponse(ApiModel):
    id: uuid.UUID
    connection_id: uuid.UUID
    github_repo_id: int
    full_name: str
    default_branch: str
    private: bool
    enabled: bool
    on_pull_request: bool
    on_push: bool
    branches: list[str]
    environment_id: uuid.UUID | None
    suite_ids: list[uuid.UUID]
    selection_mode: str
    path_rules: list[PathRule]
    status_enabled: bool = Field(description="Write a commit status to GitHub.")
    comment_enabled: bool = Field(description="Write a comment on the pull request.")
    webhook_url: str = Field(description="The payload URL to enter on GitHub.")
    created_at: datetime


class LinkSecretResponse(LinkResponse):
    webhook_secret: str = Field(description="Shown this once. Enter it as the webhook's secret.")
    webhook_events: list[str] = Field(default_factory=lambda: ["pull_request", "push"])
    webhook_content_type: str = "application/json"


class BranchResponse(ApiModel):
    name: str
    sha: str | None
    protected: bool


class MetadataResponse(ApiModel):
    repository: AvailableRepository
    branches: list[BranchResponse]


class SelectionResponse(ApiModel):
    scope: Literal["full", "affected"]
    suite_ids: list[uuid.UUID]
    reason: str
    changed_files: int
    unmatched_files: list[str]
    unmatched_count: int
    matched: dict[str, list[str]]
    decided_by: str


class PullRequestResponse(ApiModel):
    number: int
    title: str
    state: str
    draft: bool
    author: str | None
    html_url: str
    head_sha: str
    head_ref: str
    base_ref: str
    from_fork: bool
    changed_files: list[str]
    changed_files_complete: bool
    selection: SelectionResponse


class CommitResponse(ApiModel):
    sha: str
    message: str
    author: str | None
    html_url: str
    committed_at: str | None


class ImpactRequest(ApiModel):
    files: list[Annotated[str, StringConstraints(min_length=1, max_length=500)]] = Field(
        max_length=3000
    )


class DeliveryResponse(ApiModel):
    id: uuid.UUID
    delivery_id: str
    event: str
    action: str | None
    outcome: str
    reason: str | None
    subject: dict[str, Any]
    execution_ids: list[uuid.UUID]
    received_at: datetime


class WebhookAnswer(ApiModel):
    status: Literal["accepted", "ignored", "duplicate", "pong"]
    reason: str | None = None
