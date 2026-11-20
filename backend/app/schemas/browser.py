"""Browser automation: status, tool permissions and browser checks."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.schemas.common import ApiModel

CheckStatus = Literal["queued", "running", "passed", "failed"]


class ToolRuleResponse(ApiModel):
    name: str
    category: Literal["read_only", "reversible", "state_changing", "sensitive"]
    action: str | None = Field(description="Environment action this tool needs.")
    arguments: list[str] = Field(description="Arguments sessions may pass. Others are refused.")
    availability: Literal["available", "needs_approval", "forbidden", "internal"]
    enabled: bool = Field(description="False when an administrator has switched the tool off.")
    discovered: bool | None = Field(
        description="Whether the server listed this tool at the last check. Null: never checked."
    )


class BrowserConnectionResponse(ApiModel):
    status: Literal["active", "disabled", "unhealthy"]
    server_version: str | None
    last_checked_at: datetime | None
    last_error: str | None
    discovered_tools: list[str]
    unknown_tools: list[str] = Field(
        description="Tools the server offers that have no permission rule. They are never used."
    )


class BrowserStatusResponse(ApiModel):
    configured: bool
    connection: BrowserConnectionResponse | None
    restricted: bool = Field(description="True when an administrator has limited the tools.")
    tools: list[ToolRuleResponse]


class AllowedToolsUpdate(ApiModel):
    allowed_tools: list[str] | None = Field(
        max_length=100, description="Tools sessions may use. Null removes the restriction."
    )


class BrowserCheckCreate(ApiModel):
    credential_id: uuid.UUID | None = Field(
        default=None, description="Also sign in with this credential's login recipe."
    )


class BrowserCheckError(ApiModel):
    code: str
    message: str


class BrowserCheckResult(ApiModel):
    page_url: str | None
    page_title: str | None
    logged_in: bool | None = Field(description="Null when no login was requested.")
    login_steps_performed: int | None
    actions_performed: int


class BlockedRequest(ApiModel):
    host: str
    port: int
    reason: str


class BrowserCheckSession(ApiModel):
    server: str | None
    server_version: str | None
    tools_exposed: list[str]
    tools_withheld: dict[str, str]
    blocked_requests: list[BlockedRequest]


class ToolCallResponse(ApiModel):
    at: datetime
    kind: Literal["tool_call", "error"]
    tool: str | None
    category: str | None
    arguments: dict[str, Any] | None = Field(description="Sanitized: secrets are masked.")
    status: str | None
    duration_ms: int | None
    error: BrowserCheckError | None
    result_summary: str | None


class BrowserCheckResponse(ApiModel):
    id: uuid.UUID
    environment_id: uuid.UUID
    credential_id: uuid.UUID | None
    status: CheckStatus
    created_at: datetime
    finished_at: datetime | None
    result: BrowserCheckResult | None
    error: BrowserCheckError | None
    session: BrowserCheckSession | None
    tool_calls: list[ToolCallResponse]
