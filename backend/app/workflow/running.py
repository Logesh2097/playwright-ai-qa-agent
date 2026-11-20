"""Nodes 8 to 15: from approved cases to stored results and a report."""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.agent.llm.base import LLMError
from app.agent.nodes.analyse_failures import explain_failures
from app.engine.protocol import RunConfig
from app.models import TestExecution, TestReport, WorkflowRun
from app.repositories.executions import (
    EvidenceRepository,
    ExecutionRepository,
    ResultRepository,
    StepRepository,
)
from app.repositories.instructions import InstructionRepository
from app.repositories.projects import EnvironmentRepository
from app.repositories.test_cases import CaseVersionRepository, TestCaseRepository
from app.repositories.workflows import WorkflowLogRepository
from app.schemas.executions import ExecutionCreate
from app.services.audit import AuditEvent, AuditService
from app.services.automation import parse_script
from app.services.executions import ExecutionRunner, ExecutionService, WorkflowLink
from app.services.failure_analysis import as_suggestion, facts_for_model, failure_record
from app.testing.automation import changes_state
from app.testing.verdicts import execution_verdict, totals
from app.workflow.authoring import _ids, run_log
from app.workflow.deps import CaptureQueue, WorkflowDeps, WorkflowError
from app.workflow.observability import summarize
from app.workflow.report import build_report, final_statuses
from app.workflow.retry import retry_eligibility
from app.workflow.state import (
    AttemptRecord,
    EvidenceRef,
    FailureDetail,
    ReportSummary,
    ResultSummary,
    SessionRef,
    ValidationSummary,
    WorkflowState,
)

logger = logging.getLogger(__name__)

MAX_ANALYSED_FAILURES = 10
MAX_EVIDENCE_REFS = 200
_ENDED = ("completed", "failed", "cancelled", "timed_out")


def _config(state: WorkflowState) -> RunConfig:
    return RunConfig.model_validate(state["options"]["run_config"])


def _state_changes_approved(state: WorkflowState) -> bool:
    approval = state.get("approval")
    return (
        approval is not None
        and approval["decision"] == "approved"
        and "state_changing_operations" in approval["reasons"]
    )


async def _start_execution(
    state: WorkflowState, deps: WorkflowDeps, *, case_ids: list[str] | None, attempt: int
) -> str:
    """Create the execution of one attempt, or find it if an earlier try already did."""
    organization = uuid.UUID(state["organization_id"])
    async with deps.acting(*_ids(state)) as act:
        existing = (
            await act.session.execute(
                select(TestExecution).where(
                    TestExecution.organization_id == organization,
                    TestExecution.graph_thread_id == state["run_id"],
                )
            )
        ).scalars()
        for execution in existing:
            if execution.config.get("attempt") == attempt:
                return str(execution.id)
        service = ExecutionService(
            session=act.session,
            audit=act.audit,
            # The workflow runs the execution itself, in the next node.
            jobs=CaptureQueue(),
            redis=deps.redis,
            settings=deps.settings,
        )
        data = ExecutionCreate(
            environment_id=uuid.UUID(state["environment_id"]),
            suite_id=uuid.UUID(state["suite_id"])
            if case_ids is None and state.get("suite_id")
            else None,
            case_ids=[uuid.UUID(c) for c in case_ids] if case_ids is not None else None,
            config=_config(state),
        )
        view = await service.start(
            act.ctx,
            data,
            workflow=WorkflowLink(
                thread_id=state["run_id"],
                attempt=attempt,
                state_changes_approved=_state_changes_approved(state),
                instruction_id=(
                    uuid.UUID(state["instruction_id"]) if state.get("instruction_id") else None
                ),
            ),
        )
        run = await act.session.get(WorkflowRun, uuid.UUID(state["run_id"]))
        if run is not None:
            run.execution_ids = [*run.execution_ids, str(view.execution.id)]
        return str(view.execution.id)


# --- 8 -----------------------------------------------------------------------------------


async def prepare_execution(state: WorkflowState, deps: WorkflowDeps) -> dict[str, Any]:
    """Create the execution: which cases, which versions, which configuration."""
    approved = state.get("approved_test_cases", [])
    if not approved:
        raise WorkflowError("nothing_to_run", "There are no approved test cases to run.")
    use_suite = state["mode"] == "run_existing"
    execution_id = await _start_execution(
        state, deps, case_ids=None if use_suite else [c["case_id"] for c in approved], attempt=1
    )
    config = _config(state)
    return {
        "execution_id": execution_id,
        "browser_session": SessionRef(
            kind="engine_run",
            id=execution_id,
            browser=config.browser,
            viewport=config.viewport.label,
        ),
    }


# --- 9 -----------------------------------------------------------------------------------


async def execute_test_cases(state: WorkflowState, deps: WorkflowDeps) -> dict[str, Any]:
    """Run the current execution with the deterministic engine."""
    organization = uuid.UUID(state["organization_id"])
    execution_id = uuid.UUID(state["execution_id"] or "")
    runner = ExecutionRunner(
        session_factory=deps.session_factory,
        settings=deps.settings,
        redis=deps.redis,
        cipher=deps.cipher,
        engine=deps.engine,
        egress=deps.egress,
    )
    async with deps.transaction(state["organization_id"]) as session:
        execution = await ExecutionRepository(session, organization).get(execution_id)
        status = execution.status if execution else None
    if status == "queued":
        await runner.run(organization_id=organization, execution_id=execution_id)
    elif status is not None and status not in _ENDED:
        # A worker stopped while this execution was running. Its cases may have been
        # half done, so it is closed as it is and not started a second time here.
        await runner.conclude_interrupted(organization, execution_id)
    return {}


# --- 10 ----------------------------------------------------------------------------------


async def verify_test_results(state: WorkflowState, deps: WorkflowDeps) -> dict[str, Any]:
    """Read back what was stored and check it supports each status. No model."""
    organization = uuid.UUID(state["organization_id"])
    execution_id = uuid.UUID(state["execution_id"] or "")
    summaries: list[ResultSummary] = []
    failures: list[FailureDetail] = []
    inconsistent: list[str] = []
    async with deps.transaction(state["organization_id"]) as session:
        execution = await ExecutionRepository(session, organization).get(execution_id)
        if execution is None:
            raise WorkflowError("execution_missing", "The execution no longer exists.")
        results = await ResultRepository(session, organization).for_execution(execution.id)
        cases = await TestCaseRepository(session, organization).by_ids_any(
            [r.test_case_id for r in results]
        )
        versions = await CaseVersionRepository(session, organization).by_ids(
            [r.test_case_version_id for r in results]
        )
        steps = StepRepository(session, organization)
        execution_error = (execution.error or {}).get("code")
        for result in results:
            case = cases.get(result.test_case_id)
            key = case.key if case else str(result.test_case_id)
            recorded = list(await steps.for_result(result.id))
            last = max((s.attempt for s in recorded), default=0)
            final = [s for s in recorded if s.attempt == last and s.phase != "cleanup"]
            if result.status in ("passed", "flaky"):
                supported = (
                    bool(final)
                    and all(s.status == "passed" for s in final)
                    and any(s.action.startswith("assert_") for s in final)
                )
                if not supported:
                    # A pass must be backed by recorded steps. If it is not, it is not a pass.
                    result.status = "needs_review"
                    result.failure_category = "inconclusive"
                    result.failure_summary = "The recorded steps do not support a pass."
                    inconsistent.append(key)
            summaries.append(
                ResultSummary(
                    result_id=str(result.id),
                    execution_id=str(execution.id),
                    case_id=str(result.test_case_id),
                    key=key,
                    status=result.status,
                    failure_class=result.failure_class,
                    failure_summary=result.failure_summary,
                )
            )
            did_not_run = result.status == "skipped" and execution.status != "completed"
            if result.status not in ("failed", "error") and not did_not_run:
                continue
            bad = next((s for s in final if s.status in ("failed", "error")), None)
            script, _ = parse_script(
                versions[result.test_case_version_id].automation
                if result.test_case_version_id in versions
                else None
            )
            performed = [s for s in final if s.phase == "main" and s.status == "passed"]
            changed = script is not None and (
                any(changes_state(step) for step in script.steps[: len(performed)])
            )
            if execution_error == "worker_interrupted" and result.status == "error" and script:
                # The case was in progress when the worker stopped and its steps were not
                # recorded: any step that can change data may have run.
                changed = any(changes_state(step) for step in script.steps)
            eligibility = retry_eligibility(
                status=result.status,
                failure_class=result.failure_class,
                error_type=bad.error_type if bad else None,
                changed_state=changed,
                execution_error=execution_error,
            )
            failures.append(
                FailureDetail(
                    result_id=str(result.id),
                    case_id=str(result.test_case_id),
                    key=key,
                    status=result.status,
                    failure_class=result.failure_class,
                    error_type=bad.error_type if bad else None,
                    summary=result.failure_summary,
                    retry_eligible=eligibility.eligible,
                    retry_reason=eligibility.reason,
                    analysed=False,
                )
            )
        statuses = [r.status for r in results]
        validation = ValidationSummary(
            execution_id=str(execution.id),
            execution_status=execution.status,
            verdict=execution_verdict(statuses),
            totals=totals(statuses),
            inconsistent=inconsistent,
        )
        if inconsistent:
            execution.verdict = validation["verdict"]
            execution.totals = {**execution.totals, **validation["totals"]}

    # The latest result of each case replaces its earlier one.
    current = {s["case_id"] for s in summaries}
    merged = [s for s in state.get("execution_results", []) if s["case_id"] not in current]
    return {
        "execution_results": [*merged, *summaries],
        "validation_results": validation,
        "failure_details": failures,
        "pending_retry": [],
        "attempts": [
            AttemptRecord(
                attempt=len(state.get("attempts", [])) + 1,
                execution_id=str(execution_id),
                execution_status=validation["execution_status"],
                verdict=validation["verdict"],
                cases=len(summaries),
                retried=list(state.get("pending_retry", [])),
            )
        ],
    }


# --- 11 ----------------------------------------------------------------------------------


async def analyse_failures(state: WorkflowState, deps: WorkflowDeps) -> dict[str, Any]:
    """Ask a model to explain what did not pass. Stored as advice; changes no status."""
    failures = state.get("failure_details", [])
    explainable = [f for f in failures if f["status"] in ("failed", "error")][
        :MAX_ANALYSED_FAILURES
    ]
    if not explainable or not state["options"]["analyse_failures"] or not deps.gateway.configured:
        return {}
    organization = uuid.UUID(state["organization_id"])
    facts: list[dict[str, Any]] = []
    refs: dict[str, str] = {}
    categories: dict[str, str | None] = {}
    async with deps.transaction(state["organization_id"]) as session:
        results = ResultRepository(session, organization)
        steps = StepRepository(session, organization)
        versions = CaseVersionRepository(session, organization)
        for number, failure in enumerate(explainable, start=1):
            result = await results.get(uuid.UUID(failure["result_id"]))
            if result is None:
                continue
            version = await versions.get(result.test_case_version_id)
            record = failure_record(
                result,
                list(await steps.for_result(result.id)),
                case_key=failure["key"],
                title=version.title if version else "",
                expected_result=version.expected_result if version else None,
            )
            ref = f"R{number}"
            refs[ref] = failure["result_id"]
            categories[ref] = (record["classification"] or {}).get("category")
            facts.append(facts_for_model(ref, record))
    try:
        answer = await explain_failures(
            deps.gateway, facts, recorder=run_log(state, deps).llm_recorder("analyse_failures")
        )
    except LLMError as exc:
        # Advice that could not be obtained is simply absent. Nothing depends on it.
        return {"errors": [{"node": "analyse_failures", "code": exc.code, "message": str(exc)}]}

    analysed: set[str] = set()
    async with deps.transaction(state["organization_id"]) as session:
        results = ResultRepository(session, organization)
        for explanation in answer.output.explanations:
            result_id = refs.get(explanation.ref)
            result = await results.get(uuid.UUID(result_id)) if result_id else None
            if result is None:
                continue
            result.ai_analysis = as_suggestion(
                explanation, category=categories.get(explanation.ref), model=answer.model
            )
            analysed.add(result_id)  # type: ignore[arg-type]
    return {
        "failure_details": [
            {**failure, "analysed": failure["result_id"] in analysed} for failure in failures
        ]
    }


# --- 12 ----------------------------------------------------------------------------------


async def retry_eligible_tests(state: WorkflowState, deps: WorkflowDeps) -> dict[str, Any]:
    """Start another attempt for cases whose failure was transient, within the limit."""
    eligible = [f for f in state.get("failure_details", []) if f["retry_eligible"]]
    used = state.get("retry_count", 0)
    limit = state["options"]["max_retries"]
    log = run_log(state, deps)
    if not eligible or used >= limit:
        if eligible:
            await log.write(
                "node_start",
                "retry_eligible_tests",
                status="retry_limit_reached",
                detail={
                    "cases": [f["key"] for f in eligible],
                    "retries_used": used,
                    "limit": limit,
                },
            )
        return {}
    execution_id = await _start_execution(
        state, deps, case_ids=[f["case_id"] for f in eligible], attempt=used + 2
    )
    config = _config(state)
    await log.write(
        "node_start",
        "retry_eligible_tests",
        status="retry_started",
        detail={
            "retry": used + 1,
            "limit": limit,
            "execution_id": execution_id,
            "cases": [{"key": f["key"], "reason": f["retry_reason"]} for f in eligible],
        },
    )
    return {
        "execution_id": execution_id,
        "retry_count": used + 1,
        "pending_retry": [f["key"] for f in eligible],
        "browser_session": SessionRef(
            kind="engine_run",
            id=execution_id,
            browser=config.browser,
            viewport=config.viewport.label,
        ),
    }


# --- 13 ----------------------------------------------------------------------------------


async def collect_evidence(state: WorkflowState, deps: WorkflowDeps) -> dict[str, Any]:
    """Gather references to the evidence of every attempt. The files stay where they are."""
    organization = uuid.UUID(state["organization_id"])
    refs: list[EvidenceRef] = []
    async with deps.transaction(state["organization_id"]) as session:
        repository = EvidenceRepository(session, organization)
        for attempt in state.get("attempts", []):
            for item in await repository.for_execution(uuid.UUID(attempt["execution_id"])):
                refs.append(
                    EvidenceRef(
                        evidence_id=str(item.id),
                        execution_id=attempt["execution_id"],
                        result_id=str(item.result_id) if item.result_id else None,
                        kind=item.kind,
                    )
                )
    return {"evidence_refs": refs[:MAX_EVIDENCE_REFS]}


# --- 14 ----------------------------------------------------------------------------------


async def generate_report(state: WorkflowState, deps: WorkflowDeps) -> dict[str, Any]:
    """Build the report from stored facts and keep it with the run."""
    organization = uuid.UUID(state["organization_id"])
    try:
        async with deps.transaction(state["organization_id"]) as session:
            run = await session.get(WorkflowRun, uuid.UUID(state["run_id"]))
            if run is None:
                raise WorkflowError("run_missing", "The workflow run no longer exists.")
            environment = await EnvironmentRepository(session, organization).get(
                uuid.UUID(state["environment_id"])
            )
            executions = [
                execution
                for attempt in state.get("attempts", [])
                if (
                    execution := await ExecutionRepository(session, organization).get(
                        uuid.UUID(attempt["execution_id"])
                    )
                )
                is not None
            ]
            results = [
                result
                for execution in executions
                for result in await ResultRepository(session, organization).for_execution(
                    execution.id
                )
            ]
            cases = await TestCaseRepository(session, organization).by_ids_any(
                [r.test_case_id for r in results]
            )
            steps = {
                r.id: await StepRepository(session, organization).for_result(r.id) for r in results
            }
            evidence = [
                item
                for execution in executions
                for item in await EvidenceRepository(session, organization).for_execution(
                    execution.id
                )
            ]
            usage, nodes = summarize(
                await WorkflowLogRepository(session, organization).for_run(
                    run.id, run.instruction_id
                )
            )
            usage["browser_actions"] = sum(
                1 for recorded in steps.values() for step in recorded if step.status != "skipped"
            )
            usage["retries"] = state.get("retry_count", 0)
            report = build_report(
                state,
                environment={
                    "name": environment.name if environment else None,
                    "kind": environment.kind if environment else None,
                    "base_url": environment.base_url if environment else None,
                },
                executions=executions,
                results=results,
                cases=cases,
                steps=steps,
                evidence=evidence,
                usage=usage,
                nodes=nodes,
            )
            run.report = report
            run.verdict = report["verdict"]
            report_id: str | None = None
            if executions:
                row = (
                    await session.execute(
                        select(TestReport).where(
                            TestReport.organization_id == organization,
                            TestReport.execution_id == executions[0].id,
                            TestReport.format == "json",
                        )
                    )
                ).scalar_one_or_none()
                if row is None:
                    row = TestReport(
                        organization_id=organization, execution_id=executions[0].id, format="json"
                    )
                    session.add(row)
                row.status = "ready"
                row.summary = report
                row.generated_at = datetime.now(UTC)
                await session.flush()
                run.report_id = row.id
                report_id = str(row.id)
        return {
            "final_report": ReportSummary(
                status="ready", report_id=report_id, verdict=report["verdict"]
            )
        }
    except Exception as exc:
        # The results are stored whether or not a report could be written about them.
        logger.exception("workflow_report_failed", extra={"run_id": state["run_id"]})
        return {
            "final_report": ReportSummary(status="failed", report_id=None, verdict=None),
            "errors": [
                {
                    "node": "generate_report",
                    "code": getattr(exc, "code", "report_failed"),
                    "message": "The report could not be generated.",
                }
            ],
        }


async def _close_unfinished_executions(
    state: WorkflowState, deps: WorkflowDeps, *, cancelled: bool
) -> None:
    """An execution the run prepared but never finished must not stay open."""
    organization = uuid.UUID(state["organization_id"])
    async with deps.transaction(state["organization_id"]) as session:
        open_ids = [
            execution.id
            for execution in (
                await session.execute(
                    select(TestExecution).where(
                        TestExecution.organization_id == organization,
                        TestExecution.graph_thread_id == state["run_id"],
                        TestExecution.status.not_in(_ENDED),
                    )
                )
            ).scalars()
        ]
    runner = ExecutionRunner(
        session_factory=deps.session_factory,
        settings=deps.settings,
        redis=deps.redis,
        cipher=deps.cipher,
        engine=deps.engine,
        egress=deps.egress,
    )
    for execution_id in open_ids:
        await runner.close_unstarted(organization, execution_id, cancelled=cancelled)


# --- 15 ----------------------------------------------------------------------------------


async def save_execution_results(state: WorkflowState, deps: WorkflowDeps) -> dict[str, Any]:
    """Close the run: final status, verdict, and what became of the instruction."""
    organization = uuid.UUID(state["organization_id"])
    status = state.get("status", "running")
    final = "completed" if status == "running" else status
    report = state.get("final_report") or ReportSummary(
        status="failed", report_id=None, verdict=None
    )
    await _close_unfinished_executions(state, deps, cancelled=final == "cancelled")
    async with deps.transaction(state["organization_id"]) as session:
        run = await session.get(WorkflowRun, uuid.UUID(state["run_id"]))
        if run is None:
            raise WorkflowError("run_missing", "The workflow run no longer exists.")
        verdict = report["verdict"]
        if verdict is None and state.get("attempts"):
            # No report, but there are results: the verdict still comes from them.
            executions = [
                execution
                for attempt in state["attempts"]
                if (
                    execution := await ExecutionRepository(session, organization).get(
                        uuid.UUID(attempt["execution_id"])
                    )
                )
                is not None
            ]
            results = [
                r
                for execution in executions
                for r in await ResultRepository(session, organization).for_execution(execution.id)
            ]
            verdict = execution_verdict(
                [s for _, s, _ in final_statuses(executions, results).values()]
            )
        run.status = final
        run.verdict = verdict
        run.pending = None
        run.current_node = None
        run.retry_count = state.get("retry_count", 0)
        run.finished_at = datetime.now(UTC)
        errors = state.get("errors", [])
        fatal = [e for e in errors if e["node"] != "analyse_failures"]
        run.error = (
            {"code": fatal[-1]["code"], "message": fatal[-1]["message"], "node": fatal[-1]["node"]}
            if fatal and (final == "failed" or report["status"] == "failed")
            else None
        )
        if state.get("instruction_id") and final == "completed":
            instruction = await InstructionRepository(session, organization).get_for_update(
                uuid.UUID(state["instruction_id"] or "")
            )
            if instruction is not None:
                instruction.status = "completed"
        await AuditService(deps.session_factory).record(
            session,
            AuditEvent(
                action="workflow.finish",
                actor_id=uuid.UUID(state["user_id"]),
                actor_type="system",
                organization_id=organization,
                resource_type="workflow_run",
                resource_id=run.id,
                changes={
                    "status": final,
                    "verdict": verdict,
                    "retries": run.retry_count,
                    "report": report["status"],
                    "executions": [a["execution_id"] for a in state.get("attempts", [])],
                },
            ),
        )
    return {"status": "completed" if final == "completed" else status}
