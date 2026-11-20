"""Test reports and their exports, execution history, comparison, trends and the dashboard."""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import StreamingResponse

from app.api.deps import (
    DbSessionDep,
    OrgContextDep,
    ProjectContextDep,
    ProjectServiceDep,
    RedisDep,
    get_reporting_service,
)
from app.api.v1.executions import _summary
from app.core.errors import NotFoundError
from app.core.security.permissions import Permission
from app.db.context import set_current_org
from app.reporting.model import ExecutionReport
from app.repositories.executions import ExecutionRepository, ResultRepository
from app.repositories.test_cases import TestCaseRepository
from app.schemas.common import ProblemDetail
from app.schemas.executions import ExecutionSummary
from app.schemas.reports import (
    ComparisonResponse,
    DashboardResponse,
    ExecutionPageResponse,
    HistoryEntryResponse,
    LiveResult,
    LiveState,
    OrganizationDashboardResponse,
    TrendResponse,
)
from app.services.progress import read_progress
from app.services.reporting import HistoryEntry, ReportingService, organization_dashboard
from app.testing.verdicts import totals

router = APIRouter(
    prefix="/organizations/{organization_id}/projects/{project_id}", tags=["reports"]
)
org_router = APIRouter(prefix="/organizations/{organization_id}", tags=["reports"])
Reporting = Annotated[ReportingService, Depends(get_reporting_service)]

_ERR: dict[str, Any] = {"model": ProblemDetail}
_READ: dict[int | str, dict[str, Any]] = {401: _ERR, 404: _ERR, 422: _ERR}
Days = Annotated[int, Query(ge=1, le=180, description="How many days back, today included.")]
ExecutionStatus = Literal[
    "queued", "preparing", "running", "analysing", "cancelling", "completed", "failed",
    "cancelled", "timed_out",
]  # fmt: skip
Verdict = Literal["passed", "failed", "error", "flaky", "inconclusive", "needs_review"]


def _entry(entry: HistoryEntry) -> HistoryEntryResponse:
    result = entry.result
    return HistoryEntryResponse(
        result_id=result.id,
        execution_id=entry.execution_id,
        environment_id=entry.environment_id,
        case_id=result.test_case_id,
        case_key=entry.case.key if entry.case else None,
        title=entry.case.title if entry.case else None,
        status=result.status,
        failure_category=result.failure_category,
        failure_summary=result.failure_summary,
        healed=result.healed,
        attempts=result.attempt_count,
        duration_ms=result.duration_ms,
        finished_at=result.finished_at,
    )


@router.get(
    "/executions/{execution_id}/report",
    summary="The test report of an execution",
    responses=_READ,
)
async def execution_report(
    execution_id: uuid.UUID, ctx: ProjectContextDep, reporting: Reporting
) -> ExecutionReport:
    return await reporting.report(ctx, execution_id)


@router.get(
    "/executions/{execution_id}/report/export",
    summary="The same report as a JSON, CSV or PDF file",
    responses=_READ,
    response_class=Response,
)
async def export_report(
    execution_id: uuid.UUID,
    ctx: ProjectContextDep,
    reporting: Reporting,
    fmt: Annotated[Literal["json", "csv", "pdf"], Query(alias="format")] = "json",
) -> Response:
    exported = await reporting.export(ctx, execution_id, fmt)
    return Response(
        exported.content,
        media_type=exported.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{exported.filename}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


@router.get(
    "/execution-history",
    summary="Executions, newest first, with filters and paging",
    responses=_READ,
)
async def execution_history(
    ctx: ProjectContextDep,
    reporting: Reporting,
    status: Annotated[ExecutionStatus | None, Query()] = None,
    verdict: Annotated[Verdict | None, Query()] = None,
    environment_id: Annotated[uuid.UUID | None, Query()] = None,
    suite_id: Annotated[uuid.UUID | None, Query()] = None,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> ExecutionPageResponse:
    page = await reporting.executions(
        ctx, status=status, verdict=verdict, environment_id=environment_id, suite_id=suite_id,
        since=since, until=until, limit=limit, offset=offset,
    )  # fmt: skip
    return ExecutionPageResponse(
        items=[ExecutionSummary.model_validate(_summary(e)) for e in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get(
    "/execution-comparison",
    summary="What changed between two executions, case by case",
    responses=_READ,
)
async def compare_executions(
    ctx: ProjectContextDep,
    reporting: Reporting,
    base: Annotated[uuid.UUID, Query(description="The earlier execution.")],
    target: Annotated[uuid.UUID, Query(description="The execution to compare with it.")],
) -> ComparisonResponse:
    return ComparisonResponse.model_validate(await reporting.compare(ctx, base, target))


@router.get(
    "/test-cases/{case_id}/history",
    summary="Results of one test case, newest first",
    tags=["test cases"],
    responses=_READ,
)
async def case_history(
    case_id: uuid.UUID,
    ctx: ProjectContextDep,
    reporting: Reporting,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[HistoryEntryResponse]:
    return [_entry(e) for e in await reporting.case_history(ctx, case_id, limit=limit)]


@router.get(
    "/failures", summary="Results that failed or ended in an error, newest first", responses=_READ
)
async def failed_tests(
    ctx: ProjectContextDep,
    reporting: Reporting,
    days: Days = 30,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[HistoryEntryResponse]:
    return [_entry(e) for e in await reporting.failures(ctx, days=days, limit=limit)]


@router.get("/trends", summary="Executions and results per day", responses=_READ)
async def trends(ctx: ProjectContextDep, reporting: Reporting, days: Days = 30) -> TrendResponse:
    return TrendResponse.model_validate(await reporting.trends(ctx, days=days))


@router.get(
    "/dashboard",
    summary="Key figures: executions, pass and failure rates, flaky tests, coverage indicators",
    responses=_READ,
)
async def dashboard(
    ctx: ProjectContextDep, reporting: Reporting, days: Days = 30
) -> DashboardResponse:
    figures = await reporting.dashboard(ctx, days=days)
    figures["recent_executions"] = [
        ExecutionSummary.model_validate(_summary(e)) for e in figures["recent_executions"]
    ]
    return DashboardResponse.model_validate(figures)


# --- across projects ----------------------------------------------------------------------


@org_router.get(
    "/dashboard",
    summary="Figures across the projects the caller can see",
    responses=_READ,
)
async def organization_dashboard_view(
    org: OrgContextDep, projects: ProjectServiceDep, session: DbSessionDep, days: Days = 30
) -> OrganizationDashboardResponse:
    visible = [view.project for view in await projects.list(org)]
    figures = await organization_dashboard(session, visible, org.organization.id, days=days)
    figures["recent_executions"] = [
        {
            **_summary(item["execution"]),
            "project_id": item["project_id"],
            "project_name": item["project_name"],
        }
        for item in figures["recent_executions"]
    ]
    return OrganizationDashboardResponse.model_validate(figures)


# --- live progress ------------------------------------------------------------------------

STREAM_SECONDS = 30 * 60
TICK_SECONDS = 1.0
HEARTBEAT_SECONDS = 15.0
ENDED = frozenset({"completed", "failed", "cancelled", "timed_out"})


def _sse(event: str, data: Any, event_id: int | None = None) -> bytes:
    lines = [f"event: {event}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append("data: " + json.dumps(data, default=str, separators=(",", ":")))
    return ("\n".join(lines) + "\n\n").encode()


async def _live_state(
    request: Request, organization_id: uuid.UUID, project_id: uuid.UUID, execution_id: uuid.UUID
) -> tuple[dict[str, Any], bool] | None:
    """Read the execution in a transaction of its own, so the stream holds none open."""
    async with request.app.state.session_factory() as session, session.begin():
        await set_current_org(session, organization_id)
        execution = await ExecutionRepository(session, organization_id).get_in_project(
            project_id, execution_id
        )
        if execution is None:
            return None
        results = await ResultRepository(session, organization_id).for_execution(execution.id)
        cases = await TestCaseRepository(session, organization_id).by_ids_any(
            [r.test_case_id for r in results]
        )
        counts = totals([r.status for r in results])
        state = LiveState(
            id=execution.id,
            status=execution.status,
            verdict=execution.verdict,
            finished=execution.status in ENDED,
            totals={
                **counts,
                "pending": sum(1 for r in results if r.status == "pending"),
                "running": sum(1 for r in results if r.status == "running"),
            },
            started_at=execution.started_at,
            finished_at=execution.finished_at,
            results=[
                LiveResult(
                    id=r.id,
                    case_key=cases[r.test_case_id].key if r.test_case_id in cases else "",
                    title=cases[r.test_case_id].title if r.test_case_id in cases else "",
                    status=r.status,
                    attempts=r.attempt_count,
                    failure_summary=r.failure_summary,
                )
                for r in results
            ],
            current=None,
        )
        return state.model_dump(mode="json"), execution.status in ENDED


@router.get(
    "/executions/{execution_id}/live",
    summary="What is known about an execution right now",
    responses=_READ,
)
async def execution_live_state(
    execution_id: uuid.UUID, request: Request, ctx: ProjectContextDep, redis: RedisDep
) -> LiveState:
    ctx.require(Permission.PROJECTS_READ)
    found = await _live_state(request, ctx.organization_id, ctx.project.id, execution_id)
    if found is None:
        raise NotFoundError("Execution not found.")
    state, ended = found
    if not ended:
        state["current"], _ = await read_progress(redis, execution_id, 10**12)
    return LiveState.model_validate(state)


@router.get(
    "/executions/{execution_id}/events",
    summary="Live progress of an execution as server-sent events",
    responses=_READ,
    response_class=StreamingResponse,
)
async def execution_events(
    execution_id: uuid.UUID,
    request: Request,
    ctx: ProjectContextDep,
    redis: RedisDep,
    last_event_id: Annotated[int | None, Header()] = None,
) -> StreamingResponse:
    """Events: `state` (whenever it changes), `step` (each entry the engine reported), `end`.

    The stream closes itself when the execution has ended and after thirty minutes;
    a client that still wants it connects again with `Last-Event-ID`.
    """
    ctx.require(Permission.PROJECTS_READ)
    organization_id, project_id = ctx.organization_id, ctx.project.id
    if await _live_state(request, organization_id, project_id, execution_id) is None:
        raise NotFoundError("Execution not found.")

    async def stream() -> AsyncIterator[bytes]:
        seen = last_event_id or 0
        previous: str | None = None
        started = last_beat = time.monotonic()
        yield b"retry: 3000\n\n"
        while time.monotonic() - started < STREAM_SECONDS:
            if await request.is_disconnected():
                return
            found = await _live_state(request, organization_id, project_id, execution_id)
            if found is None:
                return
            state, ended = found
            current, entries = await read_progress(redis, execution_id, seen)
            for entry in entries:
                seen = int(entry["seq"])
                yield _sse("step", entry, seen)
            state["current"] = None if ended else current
            encoded = json.dumps(state, default=str, sort_keys=True)
            if encoded != previous:
                previous = encoded
                yield _sse("state", state)
                last_beat = time.monotonic()
            if ended:
                yield _sse("end", {"status": state["status"], "verdict": state["verdict"]})
                return
            if time.monotonic() - last_beat >= HEARTBEAT_SECONDS:
                last_beat = time.monotonic()
                yield b": keep-alive\n\n"
            await asyncio.sleep(TICK_SECONDS)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
