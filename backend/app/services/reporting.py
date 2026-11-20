"""Reports, exports, execution history and dashboard figures for a project.

Everything is read-only and computed from stored results at the time it is
asked for. Exports are recorded in the audit log.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import NotFoundError, ValidationFailedError
from app.core.security.permissions import Permission
from app.models import TestCase, TestExecution, TestResult
from app.reporting import analytics
from app.reporting.analytics import CaseFacts, ExecutionRow, ResultRow
from app.reporting.builder import build_execution_report
from app.reporting.exports import FORMATS, MAX_PDF_SCREENSHOTS, Export, export
from app.reporting.model import ExecutionReport
from app.repositories.executions import (
    EvidenceRepository,
    ExecutionRepository,
    ResultRepository,
)
from app.repositories.test_cases import CaseVersionRepository, TestCaseRepository
from app.services.audit import AuditEvent, AuditService
from app.services.evidence import EvidenceStorage, evidence_storage
from app.services.projects import ProjectContext

MAX_WINDOW_ROWS = 20_000
MAX_PDF_SCREENSHOT_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ExecutionPage:
    items: Sequence[TestExecution]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    result: TestResult
    execution_id: uuid.UUID
    environment_id: uuid.UUID
    case: TestCase | None


def _execution_row(execution: TestExecution) -> ExecutionRow:
    return ExecutionRow(
        id=execution.id,
        status=execution.status,
        verdict=execution.verdict,
        created_at=execution.created_at,
        started_at=execution.started_at,
        finished_at=execution.finished_at,
        environment_id=execution.environment_id,
        suite_id=execution.suite_id,
    )


def _result_row(result: TestResult, environment_id: uuid.UUID | None = None) -> ResultRow:
    return ResultRow(
        id=result.id,
        execution_id=result.execution_id,
        case_id=result.test_case_id,
        status=result.status,
        finished_at=result.finished_at,
        failure_category=result.failure_category,
        failure_class=result.failure_class,
        failure_summary=result.failure_summary,
        duration_ms=result.duration_ms,
        version_id=result.test_case_version_id,
        environment_id=environment_id,
        healed=result.healed,
        attempt_count=result.attempt_count,
    )


class ReportingService:
    def __init__(self, *, session: AsyncSession, audit: AuditService, settings: Settings) -> None:
        self._session = session
        self._audit = audit
        self._settings = settings
        self._storage: EvidenceStorage = evidence_storage(settings)

    def _executions(self, ctx: ProjectContext) -> ExecutionRepository:
        return ExecutionRepository(self._session, ctx.organization_id)

    def _results(self, ctx: ProjectContext) -> ResultRepository:
        return ResultRepository(self._session, ctx.organization_id)

    async def _execution(self, ctx: ProjectContext, execution_id: uuid.UUID) -> TestExecution:
        execution = await self._executions(ctx).get_in_project(ctx.project.id, execution_id)
        if execution is None:
            raise NotFoundError("Execution not found.")
        return execution

    # --- reports -----------------------------------------------------------------------

    async def report(self, ctx: ProjectContext, execution_id: uuid.UUID) -> ExecutionReport:
        ctx.require(Permission.PROJECTS_READ)
        execution = await self._execution(ctx, execution_id)
        return await build_execution_report(
            self._session, ctx.organization_id, ctx.project, execution
        )

    async def export(self, ctx: ProjectContext, execution_id: uuid.UUID, fmt: str) -> Export:
        """The same report as a file. PDF embeds screenshots of results that did not pass."""
        if fmt not in FORMATS:
            raise ValidationFailedError(f"Unknown report format: {fmt}.")
        report = await self.report(ctx, execution_id)
        screenshots: dict[str, bytes] = {}
        if fmt == "pdf":
            wanted = [
                item
                for result in report.results
                if result.status not in ("passed", "skipped", "blocked")
                for item in result.evidence
                if item.kind == "screenshot" and item.size_bytes <= MAX_PDF_SCREENSHOT_BYTES
            ][:MAX_PDF_SCREENSHOTS]
            keys = await self._storage_keys(ctx, execution_id, [item.id for item in wanted])
            for evidence_id, key in keys.items():
                data = self._storage.read(ctx.organization_id, key)
                if data is not None:
                    screenshots[str(evidence_id)] = data
        exported = export(report, fmt, screenshots)
        await self._audit.record(
            self._session,
            AuditEvent(
                action="report.export",
                actor_id=ctx.user.id,
                organization_id=ctx.organization_id,
                resource_type="test_execution",
                resource_id=execution_id,
                changes={"format": fmt, "bytes": len(exported.content)},
            ),
        )
        return exported

    async def _storage_keys(
        self, ctx: ProjectContext, execution_id: uuid.UUID, ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        rows = await EvidenceRepository(self._session, ctx.organization_id).for_execution(
            execution_id
        )
        wanted = set(ids)
        return {row.id: row.storage_key for row in rows if row.id in wanted}

    # --- history -----------------------------------------------------------------------

    async def executions(
        self,
        ctx: ProjectContext,
        *,
        status: str | None = None,
        verdict: str | None = None,
        environment_id: uuid.UUID | None = None,
        suite_id: uuid.UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> ExecutionPage:
        ctx.require(Permission.PROJECTS_READ)
        items, total = await self._executions(ctx).search(
            ctx.project.id,
            status=status,
            verdict=verdict,
            environment_id=environment_id,
            suite_id=suite_id,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )
        return ExecutionPage(items, total, limit, offset)

    async def _entries(
        self,
        ctx: ProjectContext,
        *,
        case_ids: Sequence[uuid.UUID] | None = None,
        statuses: Sequence[str] | None = None,
        since: datetime | None = None,
        limit: int,
    ) -> list[HistoryEntry]:
        rows = await self._results(ctx).history(
            ctx.project.id, case_ids=case_ids, statuses=statuses, since=since, limit=limit
        )
        cases = await TestCaseRepository(self._session, ctx.organization_id).by_ids_any(
            list({result.test_case_id for result, _ in rows})
        )
        return [
            HistoryEntry(
                result, result.execution_id, environment_id, cases.get(result.test_case_id)
            )
            for result, environment_id in rows
        ]

    async def case_history(
        self, ctx: ProjectContext, case_id: uuid.UUID, *, limit: int = 50
    ) -> list[HistoryEntry]:
        """Results of one case, newest first."""
        ctx.require(Permission.PROJECTS_READ)
        cases = await TestCaseRepository(self._session, ctx.organization_id).by_ids_any([case_id])
        case = cases.get(case_id)
        if case is None or case.project_id != ctx.project.id:
            raise NotFoundError("Test case not found.")
        return await self._entries(ctx, case_ids=[case_id], limit=limit)

    async def failures(
        self, ctx: ProjectContext, *, days: int = 30, limit: int = 100
    ) -> list[HistoryEntry]:
        """Results that failed or ended in an error, newest first."""
        ctx.require(Permission.PROJECTS_READ)
        since = datetime.now(UTC) - timedelta(days=days)
        return await self._entries(ctx, statuses=analytics.NOT_PASSED, since=since, limit=limit)

    # --- comparison ----------------------------------------------------------------------

    async def compare(
        self, ctx: ProjectContext, base_id: uuid.UUID, target_id: uuid.UUID
    ) -> dict[str, Any]:
        ctx.require(Permission.PROJECTS_READ)
        if base_id == target_id:
            raise ValidationFailedError("Choose two different executions to compare.")
        base = await self._execution(ctx, base_id)
        target = await self._execution(ctx, target_id)
        base_results = await self._results(ctx).for_execution(base.id)
        target_results = await self._results(ctx).for_execution(target.id)
        rows, counts = analytics.compare(
            [_result_row(r) for r in base_results], [_result_row(r) for r in target_results]
        )
        cases = await TestCaseRepository(self._session, ctx.organization_id).by_ids_any(
            [row["case_id"] for row in rows]
        )
        for row in rows:
            case = cases.get(row["case_id"])
            row["case_key"] = case.key if case else None
            row["title"] = case.title if case else None

        def side(execution: TestExecution, results: Sequence[TestResult]) -> dict[str, Any]:
            return {
                "id": execution.id,
                "status": execution.status,
                "verdict": execution.verdict,
                "environment_id": execution.environment_id,
                "started_at": execution.started_at,
                "finished_at": execution.finished_at,
                "duration_ms": _execution_row(execution).duration_ms,
                "results": analytics.result_rates([r.status for r in results]),
            }

        return {
            "base": side(base, base_results),
            "target": side(target, target_results),
            "same_environment": base.environment_id == target.environment_id,
            "changes": counts,
            "cases": rows,
        }

    # --- trends and dashboard -------------------------------------------------------------

    async def _window(
        self, ctx: ProjectContext, days: int
    ) -> tuple[list[ExecutionRow], list[ResultRow], bool]:
        """Executions and results of the last `days` days; results oldest first."""
        since = datetime.now(UTC) - timedelta(days=days)
        executions, total = await self._executions(ctx).search(
            ctx.project.id, since=since, limit=MAX_WINDOW_ROWS
        )
        rows = await self._results(ctx).history(ctx.project.id, since=since, limit=MAX_WINDOW_ROWS)
        truncated = total > len(executions) or len(rows) >= MAX_WINDOW_ROWS
        results = [_result_row(result, environment) for result, environment in reversed(rows)]
        return [_execution_row(e) for e in executions], results, truncated

    async def trends(self, ctx: ProjectContext, *, days: int = 30) -> dict[str, Any]:
        ctx.require(Permission.PROJECTS_READ)
        executions, results, truncated = await self._window(ctx, days)
        return {
            "window_days": days,
            "truncated": truncated,
            "points": analytics.trend(executions, results, days=days),
        }

    async def dashboard(self, ctx: ProjectContext, *, days: int = 30) -> dict[str, Any]:
        ctx.require(Permission.PROJECTS_READ)
        executions, results, truncated = await self._window(ctx, days)
        cases = await TestCaseRepository(self._session, ctx.organization_id).list_in_project(
            ctx.project.id, limit=5000
        )
        automated = await CaseVersionRepository(self._session, ctx.organization_id).automated_ids(
            [c.approved_version_id for c in cases if c.approved_version_id]
        )
        facts = [
            CaseFacts(
                id=case.id,
                key=case.key,
                title=case.title,
                status=case.status,
                approved=case.approved_version_id is not None,
                automated=case.approved_version_id in automated,
            )
            for case in cases
        ]
        figures = analytics.dashboard(executions, results, facts, days=days)
        recent, _ = await self._executions(ctx).search(ctx.project.id, limit=10)
        return {**figures, "truncated": truncated, "recent_executions": list(recent)}


MAX_DASHBOARD_PROJECTS = 50


async def organization_dashboard(
    session: AsyncSession, projects: Sequence[Any], organization_id: uuid.UUID, *, days: int
) -> dict[str, Any]:
    """Figures across the projects the caller can see. `projects` is that list, already checked."""
    since = datetime.now(UTC) - timedelta(days=days)
    executions_repo = ExecutionRepository(session, organization_id)
    results_repo = ResultRepository(session, organization_id)
    cases_repo = TestCaseRepository(session, organization_id)
    executions: list[ExecutionRow] = []
    results: list[ResultRow] = []
    recent: list[tuple[TestExecution, Any]] = []
    facts: dict[Any, CaseFacts] = {}
    project_of_case: dict[Any, Any] = {}
    truncated = len(projects) > MAX_DASHBOARD_PROJECTS
    for project in projects[:MAX_DASHBOARD_PROJECTS]:
        found, total = await executions_repo.search(project.id, since=since, limit=2000)
        rows = await results_repo.history(project.id, since=since, limit=5000)
        truncated = truncated or total > len(found) or len(rows) >= 5000
        executions += [_execution_row(e) for e in found]
        results += [_result_row(result, environment) for result, environment in rows]
        recent += [(e, project) for e in found[:10]]
        cases = await cases_repo.by_ids_any(list({r.test_case_id for r, _ in rows}))
        for case in cases.values():
            project_of_case[case.id] = project
            facts[case.id] = CaseFacts(
                id=case.id,
                key=case.key,
                title=case.title,
                status=case.status,
                approved=case.approved_version_id is not None,
            )
    # Oldest first, as the analytics expect.
    results.sort(key=lambda row: row.finished_at or since)
    recent.sort(key=lambda pair: pair[0].created_at, reverse=True)
    failing = analytics.frequently_failing(results, facts)
    for item in failing:
        project = project_of_case.get(item["case_id"])
        item["project_id"] = project.id if project else None
        item["project_name"] = project.name if project else None
    return {
        "window_days": days,
        "truncated": truncated,
        "total_projects": len(projects),
        "active_projects": sum(1 for p in projects if p.status == "active"),
        "total_executions": len(executions),
        "finished_executions": sum(1 for e in executions if e.verdict is not None),
        "results": analytics.result_rates([row.status for row in results]),
        "points": analytics.trend(executions, results, days=days),
        "frequently_failing": failing,
        "recent_executions": [
            {"execution": execution, "project_id": project.id, "project_name": project.name}
            for execution, project in recent[:10]
        ],
    }
