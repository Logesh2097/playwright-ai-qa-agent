"""Browser automation: what it may do, and checks against an environment."""

import uuid
from collections.abc import Sequence
from typing import Annotated, Any

from fastapi import APIRouter, Depends, status

from app.api.deps import OrgContextDep, ProjectContextDep, get_browser_check_service
from app.api.v1.projects import EnvironmentDep
from app.browser.policy import ToolCategory, ToolRule
from app.models import AgentExecutionLog
from app.schemas.browser import (
    AllowedToolsUpdate,
    BrowserCheckCreate,
    BrowserCheckError,
    BrowserCheckResponse,
    BrowserConnectionResponse,
    BrowserStatusResponse,
    ToolCallResponse,
    ToolRuleResponse,
)
from app.schemas.common import ProblemDetail
from app.services.browser_checks import BrowserCheckService, BrowserStatus

router = APIRouter(prefix="/organizations/{organization_id}", tags=["browser"])

ServiceDep = Annotated[BrowserCheckService, Depends(get_browser_check_service)]
_ERR: dict[str, Any] = {"model": ProblemDetail}
_CHECKS = (
    "/projects/{project_id}/applications/{application_id}"
    "/environments/{environment_id}/browser-checks"
)


def _availability(rule: ToolRule) -> str:
    if rule.internal:
        return "internal"
    if rule.category is ToolCategory.SENSITIVE:
        return "needs_approval" if rule.grantable else "forbidden"
    return "available"


def _status(view: BrowserStatus) -> BrowserStatusResponse:
    connection = view.connection
    restricted = set(connection.allowed_tools) if connection and connection.allowed_tools else None
    discovered: set[str] | None = None
    if connection is not None and connection.discovered_tools is not None:
        discovered = {str(tool.get("name")) for tool in connection.discovered_tools}
    known = {rule.name for rule in view.policy.rules()}
    return BrowserStatusResponse(
        configured=view.configured,
        restricted=restricted is not None,
        connection=(
            BrowserConnectionResponse(
                status=connection.status,
                server_version=connection.server_version,
                last_checked_at=connection.last_health_check_at,
                last_error=connection.last_error,
                discovered_tools=sorted(discovered or ()),
                unknown_tools=sorted((discovered or set()) - known),
            )
            if connection is not None
            else None
        ),
        tools=[
            ToolRuleResponse(
                name=rule.name,
                category=rule.category.value,
                action=rule.action,
                arguments=sorted(rule.allowed_args),
                availability=_availability(rule),
                enabled=restricted is None or rule.name in restricted,
                discovered=None if discovered is None else rule.name in discovered,
            )
            for rule in view.policy.rules()
        ],
    )


def _call(row: AgentExecutionLog) -> ToolCallResponse:
    args = row.tool_args or {}
    return ToolCallResponse(
        at=row.created_at,
        kind=row.event_type,
        tool=row.tool_name,
        category=args.get("category"),
        arguments=args.get("arguments"),
        status=row.status,
        duration_ms=row.latency_ms,
        error=(
            BrowserCheckError(
                code=str(row.error.get("code")), message=str(row.error.get("message") or "")
            )
            if row.error
            else None
        ),
        result_summary=row.tool_result_summary,
    )


def _check(state: dict[str, Any], calls: Sequence[AgentExecutionLog]) -> BrowserCheckResponse:
    return BrowserCheckResponse.model_validate(
        {
            "id": state["id"],
            "environment_id": state["environment_id"],
            "credential_id": state.get("credential_id"),
            "status": state["status"],
            "created_at": state["created_at"],
            "finished_at": state.get("finished_at"),
            "result": state.get("result"),
            "error": state.get("error"),
            "session": state.get("session"),
            "tool_calls": [_call(row) for row in calls],
        }
    )


@router.get(
    "/browser",
    summary="Browser automation status and tool permissions",
    responses={401: _ERR, 404: _ERR},
)
async def browser_status(ctx: OrgContextDep, service: ServiceDep) -> BrowserStatusResponse:
    """Which browser tools exist, how each is classified, and what the last check found."""
    return _status(await service.status(ctx))


@router.put(
    "/browser/allowed-tools",
    summary="Restrict the browser tools this organization uses",
    responses={401: _ERR, 403: _ERR, 404: _ERR, 422: _ERR},
)
async def set_allowed_tools(
    body: AllowedToolsUpdate, ctx: OrgContextDep, service: ServiceDep
) -> BrowserStatusResponse:
    """Organization admins only. Can switch tools off; cannot enable a forbidden one."""
    return _status(await service.set_allowed_tools(ctx, body.allowed_tools))


@router.post(
    _CHECKS,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Open the environment in a browser, optionally signing in",
    responses={401: _ERR, 403: _ERR, 404: _ERR, 409: _ERR, 422: _ERR, 429: _ERR, 503: _ERR},
)
async def start_browser_check(
    body: BrowserCheckCreate,
    ctx: ProjectContextDep,
    environment: EnvironmentDep,
    service: ServiceDep,
) -> BrowserCheckResponse:
    """Runs in the background; poll the returned check until it is `passed` or `failed`."""
    state = await service.start(ctx, environment, credential_id=body.credential_id)
    return _check(state, [])


@router.get(
    _CHECKS + "/{check_id}",
    summary="Result of a browser check",
    responses={401: _ERR, 403: _ERR, 404: _ERR},
)
async def get_browser_check(
    check_id: uuid.UUID, ctx: ProjectContextDep, environment: EnvironmentDep, service: ServiceDep
) -> BrowserCheckResponse:
    state, calls = await service.get(ctx, environment, check_id)
    return _check(state, calls)
