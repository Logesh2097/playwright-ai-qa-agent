"""Response structures shared by all endpoints."""

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    """Base for request and response bodies: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)


class ErrorItem(ApiModel):
    field: str | None = Field(default=None, description="Location of the problem, if any.")
    code: str
    message: str


class ProblemDetail(ApiModel):
    """Error envelope returned by every failing request (application/problem+json)."""

    type: str
    title: str
    status: int
    code: str = Field(description="Stable machine-readable error code.")
    detail: str | None = None
    errors: list[ErrorItem] = Field(default_factory=list)
    request_id: str | None = None


class Page[T](ApiModel):
    """Cursor-paginated list response."""

    items: list[T]
    next_cursor: str | None = None
