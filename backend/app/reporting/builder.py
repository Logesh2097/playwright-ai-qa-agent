"""Build an `ExecutionReport` from what is stored about an execution.

`assemble` is pure: it takes loaded rows and returns the report, so it can be
checked without a database. `build_execution_report` loads the rows.
"""

import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Application,
    Environment,
    Project,
    TestCase,
    TestCaseVersion,
    TestEvidence,
    TestExecution,
    TestExecutionStep,
    TestInstruction,
    TestResult,
    TestSuite,
    User,
)
from app.reporting.model import (
    SUGGESTION_NOTE,
    EnvironmentInfo,
    EvidenceRef,
    ExecutionInfo,
    ExecutionReport,
    FailureSummary,
    InstructionInfo,
    Named,
    ResultDetail,
    StepDetail,
    Totals,
)
from app.repositories.executions import EvidenceRepository, ResultRepository, StepRepository
from app.repositories.test_cases import CaseVersionRepository, TestCaseRepository
from app.testing.verdicts import totals as count_totals

# Statuses of a case that ran to an end; the base of the pass rate.
DECISIVE = ("passed", "failed", "error", "flaky", "needs_review")
NOT_PASSED = ("failed", "error")


def pass_rate(counts: Mapping[str, int]) -> float | None:
    """Share of the cases that ran to an end which passed first time, as written."""
    ran = sum(counts.get(status, 0) for status in DECISIVE)
    return round(counts.get("passed", 0) / ran, 4) if ran else None


def duration_ms(started: datetime | None, finished: datetime | None) -> int | None:
    if started is None or finished is None:
        return None
    return max(0, int((finished - started).total_seconds() * 1000))


def _step(step: TestExecutionStep) -> StepDetail:
    return StepDetail(
        attempt=step.attempt,
        position=step.position,
        phase=step.phase,
        action=step.action,
        description=step.description,
        status=step.status,
        started_at=step.started_at,
        finished_at=step.finished_at,
        duration_ms=step.duration_ms,
        locator=step.resolved_locator,
        healed_from=step.healed_from,
        expected=step.expected,
        actual=step.actual,
        error_type=step.error_type,
        error_message=step.error_message,
        page_url=step.page_url,
    )


def assemble(
    *,
    project: Project,
    application: Application | None,
    environment: Environment | None,
    instruction: TestInstruction | None,
    suite: TestSuite | None,
    execution: TestExecution,
    triggered_by: User | None,
    results: Sequence[TestResult],
    cases: Mapping[uuid.UUID, TestCase],
    versions: Mapping[uuid.UUID, TestCaseVersion],
    steps: Mapping[uuid.UUID, Sequence[TestExecutionStep]],
    evidence: Sequence[TestEvidence],
    now: datetime | None = None,
) -> ExecutionReport:
    moment = now or datetime.now(UTC)
    usable = [
        item
        for item in evidence
        if item.redaction_status != "failed_quarantined"
        and (item.expires_at is None or item.expires_at > moment)
    ]
    expired = len(evidence) - len(usable)

    details: list[ResultDetail] = []
    failures: list[FailureSummary] = []
    for result in results:
        case = cases.get(result.test_case_id)
        version = versions.get(result.test_case_version_id)
        recorded = list(steps.get(result.id, ()))
        positions = {step.id: step.position for step in recorded}
        files = [
            EvidenceRef(
                id=item.id,
                kind=item.kind,
                content_type=item.content_type,
                size_bytes=item.size_bytes,
                step_position=positions.get(item.execution_step_id)
                if item.execution_step_id
                else None,
                expires_at=item.expires_at,
            )
            for item in usable
            if item.result_id == result.id
        ]
        observed = result.details or {}
        detail = ResultDetail(
            id=result.id,
            case_id=result.test_case_id,
            case_key=case.key if case else str(result.test_case_id),
            title=version.title if version else (case.title if case else ""),
            version=version.version if version else 0,
            expected_result=version.expected_result if version else None,
            status=result.status,
            failure_category=result.failure_category,
            failure_summary=result.failure_summary,
            healed=result.healed,
            attempts=result.attempt_count,
            started_at=result.started_at,
            finished_at=result.finished_at,
            duration_ms=result.duration_ms,
            console_errors=[str(item) for item in observed.get("console_errors", [])],
            page_errors=[str(item) for item in observed.get("page_errors", [])],
            network_errors=[
                *observed.get("failed_requests", []),
                *({**item, "blocked": True} for item in observed.get("blocked_requests", [])),
            ],
            verification=observed.get("verification"),
            steps=[_step(step) for step in recorded],
            evidence=files,
            ai_suggestion=(
                {**result.ai_analysis, "advisory": True, "note": SUGGESTION_NOTE}
                if result.ai_analysis
                else None
            ),
        )
        details.append(detail)
        if result.status in NOT_PASSED:
            last = max((step.attempt for step in recorded), default=0)
            bad = next(
                (
                    step
                    for step in recorded
                    if step.attempt == last
                    and step.phase != "cleanup"
                    and step.status in NOT_PASSED
                ),
                None,
            )
            failures.append(
                FailureSummary(
                    result_id=result.id,
                    case_key=detail.case_key,
                    title=detail.title,
                    status=result.status,
                    failure_category=result.failure_category,
                    summary=result.failure_summary,
                    failed_step=(
                        f"Step {bad.position}: {bad.description or bad.action}" if bad else None
                    ),
                    expected=bad.expected if bad else None,
                    actual=bad.actual if bad else None,
                    screenshot_ids=[
                        item.id
                        for item in usable
                        if item.result_id == result.id and item.kind == "screenshot"
                    ],
                )
            )

    counts = count_totals([result.status for result in results])
    notes = [
        "Counts and statuses are computed from the stored results when the report is generated.",
        "Pass rate is the share of cases that ran to an end and passed first time, as written. "
        "Flaky and needs-review results are not counted as passed.",
        "This report describes test results. It does not measure code coverage.",
    ]
    if any(detail.ai_suggestion for detail in details):
        notes.append(SUGGESTION_NOTE)
    if expired:
        notes.append(f"{expired} evidence file(s) are no longer available (retention ended).")
    if execution.finished_at is None:
        notes.append("The execution has not finished; this report is provisional.")

    config = dict(execution.config or {})
    return ExecutionReport(
        generated_at=moment,
        project=Named(id=project.id, name=project.name),
        application=Named(id=application.id, name=application.name) if application else None,
        environment=(
            EnvironmentInfo(
                id=environment.id,
                name=environment.name,
                kind=environment.kind,
                base_url=environment.base_url,
            )
            if environment
            else None
        ),
        instruction=(
            InstructionInfo(id=instruction.id, text=instruction.raw_text) if instruction else None
        ),
        suite=Named(id=suite.id, name=suite.name) if suite else None,
        execution=ExecutionInfo(
            id=execution.id,
            status=execution.status,
            verdict=execution.verdict,
            started_at=execution.started_at,
            finished_at=execution.finished_at,
            duration_ms=duration_ms(execution.started_at, execution.finished_at),
            triggered_by=triggered_by.full_name or triggered_by.email if triggered_by else None,
            browser=config.get("browser"),
            config=config,
            error=execution.error,
        ),
        totals=Totals(**counts, pass_rate=pass_rate(counts)),
        failure_categories=dict(Counter(r.failure_category for r in results if r.failure_category)),
        failures=failures,
        results=details,
        notes=notes,
    )


async def build_execution_report(
    session: AsyncSession,
    organization_id: uuid.UUID,
    project: Project,
    execution: TestExecution,
) -> ExecutionReport:
    """Load everything about one execution of this project and assemble its report."""
    results = await ResultRepository(session, organization_id).for_execution(execution.id)
    cases = await TestCaseRepository(session, organization_id).by_ids_any(
        [r.test_case_id for r in results]
    )
    versions = await CaseVersionRepository(session, organization_id).by_ids(
        [r.test_case_version_id for r in results]
    )
    step_rows = await StepRepository(session, organization_id).for_execution(execution.id)
    steps: dict[uuid.UUID, list[TestExecutionStep]] = {}
    for step in step_rows:
        steps.setdefault(step.result_id, []).append(step)
    evidence = await EvidenceRepository(session, organization_id).for_execution(execution.id)

    async def owned(model: Any, entity_id: uuid.UUID | None) -> Any:
        """A row of this organization, or None. Deleted rows are still named in a report."""
        if entity_id is None:
            return None
        row = await session.get(model, entity_id)
        return row if row is not None and row.organization_id == organization_id else None

    environment = await owned(Environment, execution.environment_id)
    application = await owned(Application, environment.application_id if environment else None)
    triggered_by = (
        await session.get(User, execution.triggered_by) if execution.triggered_by else None
    )
    return assemble(
        project=project,
        application=application,
        environment=environment,
        instruction=await owned(TestInstruction, execution.instruction_id),
        suite=await owned(TestSuite, execution.suite_id),
        execution=execution,
        triggered_by=triggered_by,
        results=results,
        cases=cases,
        versions=versions,
        steps=steps,
        evidence=evidence,
    )
