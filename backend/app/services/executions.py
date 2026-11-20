"""Test executions: running approved cases with the deterministic engine.

`ExecutionService` (API side) decides what will run and records the request.
`ExecutionRunner` (worker side) builds the plan, drives the engine, and writes
down what happened: every step, every result, the evidence, and the verdict.

No model is called anywhere in this module. A result's status comes from the
engine's step outcomes; the execution's verdict from `app.testing.verdicts`.
"""

import asyncio
import base64
import contextlib
import json
import logging
import secrets
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.browser.errors import BrowserError
from app.browser.redaction import Redactor
from app.browser.services import EgressClient
from app.core.config import Settings
from app.core.errors import (
    ConflictError,
    ErrorDetail,
    ForbiddenError,
    NotFoundError,
    RateLimitedError,
    ServiceUnavailableError,
    ValidationFailedError,
)
from app.core.jobs import JobQueue
from app.core.metrics import metrics
from app.core.rate_limit import RateLimiter
from app.core.redis import RedisClient
from app.core.security.crypto import CredentialCipher
from app.core.security.permissions import Permission, has_permission
from app.core.security.url_safety import DestinationPolicy
from app.db.context import set_current_org
from app.engine.client import EngineClient, EngineError
from app.engine.protocol import (
    CaseFinished,
    CaseStarted,
    CredentialGrant,
    FileGrant,
    PlannedCase,
    ProxySettings,
    RunConfig,
    RunFinished,
    RunPlan,
    StepFinished,
)
from app.models import (
    Environment,
    LocatorHealProposal,
    Project,
    TestCase,
    TestCaseVersion,
    TestEvidence,
    TestExecution,
    TestExecutionStep,
    TestResult,
)
from app.models.execution import ACTIVE_EXECUTION_STATUSES
from app.repositories.executions import (
    EvidenceRepository,
    ExecutionRepository,
    HealProposalRepository,
    ResultRepository,
    StepRepository,
)
from app.repositories.projects import ApplicationRepository, EnvironmentRepository
from app.repositories.test_cases import CaseVersionRepository, SuiteRepository, TestCaseRepository
from app.repositories.test_data import TestFileRepository
from app.schemas.executions import ExecutionCreate
from app.services.audit import AuditEvent, AuditService
from app.services.automation import EnvironmentFacts, load_environment_facts, parse_script
from app.services.credentials import CredentialService
from app.services.evidence import (
    KINDS,
    EvidenceLimits,
    EvidenceRejectedError,
    EvidenceStore,
    StoredFile,
    validate,
)
from app.services.progress import ProgressWriter
from app.services.projects import ProjectContext
from app.services.test_cases import content_of
from app.testing.automation import Script, changes_state, placeholders_in
from app.testing.automation_checks import check_script
from app.testing.cases import CaseContent
from app.testing.classification import classify, verification_summary
from app.testing.sensitive import SensitiveStep, sensitive_steps
from app.testing.verdicts import execution_verdict, totals

logger = logging.getLogger(__name__)

JOB_NAME = "run_execution"
ENDED = frozenset({"completed", "failed", "cancelled", "timed_out"})


def cancel_key(execution_id: uuid.UUID) -> str:
    return f"execution:cancel:{execution_id}"


@dataclass(frozen=True, slots=True)
class Automation:
    """Who started an execution when it was not a signed-in person."""

    trigger: str  # "ci" or "github_webhook"
    actor_type: str  # "api_key" or "system"
    actor_id: uuid.UUID | None = None
    api_key_id: uuid.UUID | None = None
    # Repository, commit, branch, pull request: what the run was for. No secrets.
    git_context: dict[str, Any] | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowLink:
    """Ties an execution to the workflow run that started it."""

    thread_id: str
    attempt: int
    state_changes_approved: bool
    # The instruction the workflow was started from, if any.
    instruction_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class Selected:
    """One case chosen for an execution, with the version that will run."""

    case: TestCase
    version: TestCaseVersion
    content: CaseContent
    script: Script | None
    # Why it will not run, if it will not.
    blocked: str | None = None
    skipped: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionView:
    execution: TestExecution
    results: Sequence[TestResult]
    cases: dict[uuid.UUID, TestCase]
    versions: dict[uuid.UUID, TestCaseVersion]


async def select_cases(
    session: AsyncSession,
    organization_id: uuid.UUID,
    facts: EnvironmentFacts,
    pairs: Sequence[tuple[TestCase, uuid.UUID]],
) -> list[Selected]:
    """Pair each case with its approved version and decide whether it can run here."""
    versions = CaseVersionRepository(session, organization_id)
    by_id = await versions.by_ids([version_id for _, version_id in pairs])
    steps = await versions.steps(list(by_id))
    selected: list[Selected] = []
    for case, version_id in pairs:
        version = by_id[version_id]
        content = content_of(version, steps.get(version.id, []))
        script, issues = parse_script(version.automation)
        if version.automation is None:
            selected.append(
                Selected(case, version, content, None, skipped="The case has no automation script.")
            )
            continue
        if script is not None:
            issues = check_script(script, facts.script_facts(script, content))
        blocking = [issue.message for issue in issues if issue.severity == "blocking"]
        selected.append(
            Selected(case, version, content, script, blocked=blocking[0] if blocking else None)
        )
    return selected


class ExecutionService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        audit: AuditService,
        jobs: JobQueue,
        redis: RedisClient,
        settings: Settings,
    ) -> None:
        self._session = session
        self._audit = audit
        self._jobs = jobs
        self._redis = redis
        self._settings = settings
        self._limiter = RateLimiter(redis)
        self._store = EvidenceStore(settings.evidence_root)

    # --- starting ----------------------------------------------------------------------

    async def start(
        self,
        ctx: ProjectContext,
        data: ExecutionCreate,
        *,
        workflow: "WorkflowLink | None" = None,
    ) -> ExecutionView:
        """`workflow` is given when the agent workflow starts the execution as one of its attempts."""
        ctx.require(Permission.EXECUTIONS_RUN)
        ctx.require_writable()
        if data.approve_sensitive_actions and workflow is None:
            # Approving a sensitive step is the same kind of decision as approving a test case.
            ctx.require(Permission.CASES_APPROVE)
        view, _ = await self._create(
            ctx.organization_id,
            ctx.project.id,
            data,
            workflow=workflow,
            # The workflow has its own approval step, with the same rule about who may approve.
            approve=(
                (lambda environment, selected: workflow.state_changes_approved)
                if workflow is not None
                else (lambda environment, selected: self._approval(ctx, environment, selected))
            ),
            trigger="manual",
            triggered_by=ctx.user.id,
            actor_type="user",
            actor_id=ctx.user.id,
            approve_sensitive=(
                # A workflow carries the decision of its own approval step, or none.
                (True if workflow.state_changes_approved else None)
                if workflow is not None
                else data.approve_sensitive_actions
            ),
        )
        return view

    async def start_automated(
        self,
        *,
        organization_id: uuid.UUID,
        project: Project,
        data: ExecutionCreate,
        automation: "Automation",
    ) -> tuple[ExecutionView, bool]:
        """Start an execution for a CI system or a webhook. Returns the view and whether it is new.

        The same rules as for a person apply (approved work only, limits), with two
        differences: nothing automated can approve a run that changes data on an
        environment that requires approval, and a repeated request with the same
        idempotency key gets the execution that already exists.
        """
        if project.status != "active":
            raise ConflictError("This project is archived.", code="project_archived")
        if automation.idempotency_key is not None:
            existing = await ExecutionRepository(
                self._session, organization_id
            ).get_by_idempotency_key(automation.idempotency_key)
            if existing is not None:
                if existing.project_id != project.id:
                    raise ConflictError(
                        "This idempotency key was used for another project.",
                        code="idempotency_conflict",
                    )
                return await self._view(organization_id, existing), False

        def approve(environment: Environment, selected: Sequence[Selected]) -> bool:
            if environment.requires_approval and self._changes_state(selected):
                raise ForbiddenError(
                    "This environment requires an administrator's approval for tests that change "
                    "data or sign in. An automated trigger cannot give that approval.",
                    code="approval_required",
                )
            return False

        return await self._create(
            organization_id,
            project.id,
            data,
            workflow=None,
            approve=approve,
            trigger=automation.trigger,
            triggered_by=None,
            actor_type=automation.actor_type,
            actor_id=automation.actor_id,
            api_key_id=automation.api_key_id,
            git_context=automation.git_context,
            idempotency_key=automation.idempotency_key,
        )

    async def _view(self, organization_id: uuid.UUID, execution: TestExecution) -> ExecutionView:
        results = await ResultRepository(self._session, organization_id).for_execution(execution.id)
        cases = await TestCaseRepository(self._session, organization_id).by_ids_any(
            [r.test_case_id for r in results]
        )
        versions = await CaseVersionRepository(self._session, organization_id).by_ids(
            [r.test_case_version_id for r in results]
        )
        return ExecutionView(execution, results, cases, versions)

    async def _create(
        self,
        organization_id: uuid.UUID,
        project_id: uuid.UUID,
        data: ExecutionCreate,
        *,
        workflow: "WorkflowLink | None",
        approve: Callable[[Environment, Sequence[Selected]], bool],
        trigger: str,
        triggered_by: uuid.UUID | None,
        actor_type: str,
        actor_id: uuid.UUID | None,
        api_key_id: uuid.UUID | None = None,
        git_context: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        # None: nobody was asked (automation), so sensitive cases are blocked.
        # False: a person started the run without approving: they are told what needs it.
        approve_sensitive: bool | None = None,
    ) -> tuple[ExecutionView, bool]:
        if not self._settings.engine_configured:
            raise ServiceUnavailableError(
                "The test engine is not configured on this installation.",
                code="engine_not_configured",
            )
        environment = await self._environment(organization_id, project_id, data.environment_id)
        pairs = await self._selection(organization_id, project_id, data)
        facts = await load_environment_facts(
            self._session,
            organization_id=organization_id,
            project_id=project_id,
            environment=environment,
        )
        selected = await select_cases(self._session, organization_id, facts, pairs)
        selected, sensitive = self._sensitive(environment, selected, approve_sensitive)
        approved = approve(environment, selected)

        executions = ExecutionRepository(self._session, organization_id)
        if await executions.count_active() >= self._settings.execution_max_active:
            raise RateLimitedError(
                "Too many executions are queued or running. Wait for one to finish.",
                code="too_many_executions",
            )
        await self._limiter.enforce(
            "execution",
            str(organization_id),
            limit=self._settings.execution_limit,
            window_s=self._settings.execution_window_s,
            message="Too many executions were started. Please try again later.",
        )

        now = datetime.now(UTC)
        runnable = [s for s in selected if s.script is not None and not s.blocked]
        execution = TestExecution(
            organization_id=organization_id,
            project_id=project_id,
            environment_id=environment.id,
            suite_id=data.suite_id,
            instruction_id=workflow.instruction_id if workflow is not None else None,
            trigger=trigger,
            triggered_by=triggered_by,
            api_key_id=api_key_id,
            git_context=git_context,
            idempotency_key=idempotency_key,
            status="queued" if runnable else "completed",
            config={
                **data.config.model_dump(mode="json"),
                "state_changes_approved": approved,
                "sensitive_actions_approved": bool(approve_sensitive) and bool(sensitive),
                **({"attempt": workflow.attempt} if workflow is not None else {}),
            },
            graph_thread_id=workflow.thread_id if workflow is not None else None,
            timeout_s=self._settings.execution_timeout_s,
            queued_at=now,
            finished_at=None if runnable else now,
        )
        await executions.add(execution)
        results = [
            TestResult(
                organization_id=organization_id,
                execution_id=execution.id,
                test_case_id=item.case.id,
                test_case_version_id=item.version.id,
                browser=data.config.browser,
                viewport=data.config.viewport.label,
                status="blocked" if item.blocked else "skipped" if item.skipped else "pending",
                failure_class="test_data" if item.blocked else None,
                failure_summary=item.blocked or item.skipped,
            )
            for item in selected
        ]
        self._session.add_all(results)
        await self._session.flush()
        if not runnable:
            # Nothing can run, so nothing was verified: never a pass.
            _conclude(execution, results)
        await self._audit.record(
            self._session,
            AuditEvent(
                action="execution.start",
                actor_id=actor_id,
                actor_type=actor_type,
                organization_id=organization_id,
                resource_type="test_execution",
                resource_id=execution.id,
                changes={
                    "environment_id": str(environment.id),
                    "suite_id": str(data.suite_id) if data.suite_id else None,
                    "cases": len(selected),
                    "runnable": len(runnable),
                    "state_changes_approved": approved,
                    "sensitive_actions_approved": [
                        {"case": key, **step.as_dict()}
                        for key, steps in sensitive.items()
                        for step in steps
                    ]
                    if approve_sensitive
                    else [],
                    "trigger": trigger,
                },
            ),
        )
        if runnable:
            await self._jobs.enqueue(
                JOB_NAME, organization_id=str(organization_id), execution_id=str(execution.id)
            )
        view = ExecutionView(
            execution,
            results,
            {s.case.id: s.case for s in selected},
            {s.version.id: s.version for s in selected},
        )
        return view, True

    async def _environment(
        self, organization_id: uuid.UUID, project_id: uuid.UUID, environment_id: uuid.UUID
    ) -> Environment:
        environment = await EnvironmentRepository(self._session, organization_id).get(
            environment_id
        )
        application = (
            await ApplicationRepository(self._session, organization_id).get(
                environment.application_id
            )
            if environment
            else None
        )
        if environment is None or application is None or application.project_id != project_id:
            raise NotFoundError("Environment not found.")
        if environment.status != "active":
            raise ConflictError("This environment is not active.", code="environment_inactive")
        return environment

    async def _selection(
        self, organization_id: uuid.UUID, project_id: uuid.UUID, data: ExecutionCreate
    ) -> list[tuple[TestCase, uuid.UUID]]:
        """Only approved work runs: an approved suite's pinned versions, or approved cases."""
        cases = TestCaseRepository(self._session, organization_id)
        pairs: list[tuple[TestCase, uuid.UUID]] = []
        if data.suite_id is not None:
            suites = SuiteRepository(self._session, organization_id)
            suite = await suites.get_in_project(project_id, data.suite_id)
            if suite is None:
                raise NotFoundError("Test suite not found.")
            if suite.status != "approved":
                raise ConflictError("Only an approved suite can be run.", code="suite_not_approved")
            for member in (await suites.members([suite.id]))[suite.id]:
                case = await cases.get_in_project(project_id, member.test_case_id)
                if case is not None and member.approved_version_id is not None:
                    pairs.append((case, member.approved_version_id))
        else:
            for case_id in dict.fromkeys(data.case_ids or []):
                case = await cases.get_in_project(project_id, case_id)
                if case is None:
                    raise NotFoundError("Test case not found.")
                if case.approved_version_id is None:
                    raise ConflictError(
                        f"{case.key} is not approved. Only approved cases can be run.",
                        code="case_not_approved",
                    )
                pairs.append((case, case.approved_version_id))
        if not pairs:
            raise ConflictError("There is nothing to run.", code="nothing_to_run")
        if len(pairs) > self._settings.execution_max_cases:
            raise ValidationFailedError(
                f"An execution can run at most {self._settings.execution_max_cases} cases.",
                code="too_many_cases",
            )
        return pairs

    @staticmethod
    def _approval(
        ctx: ProjectContext, environment: Environment, selected: Sequence[Selected]
    ) -> bool:
        """On an approval-required environment, only an admin may start a run that changes data."""
        if not environment.requires_approval:
            return False
        if not ExecutionService._changes_state(selected):
            return False
        if not has_permission(ctx.org.role, Permission.ENVIRONMENTS_SECURITY):
            raise ForbiddenError(
                "This environment requires approval for tests that change data or sign in. "
                "Ask an organization admin to start the execution.",
                code="approval_required",
            )
        return True

    @staticmethod
    def _sensitive(
        environment: Environment, selected: Sequence[Selected], approved: bool | None
    ) -> tuple[list[Selected], dict[str, list[SensitiveStep]]]:
        """Apply the rules for sensitive steps. Returns the cases and what was approved.

        Production: never run. Elsewhere: run only with a person's approval for this
        execution; without anyone to ask, the case is blocked and the others go on.
        """
        found: dict[str, list[SensitiveStep]] = {}
        result: list[Selected] = []
        for item in selected:
            steps = (
                sensitive_steps(item.script, item.content)
                if item.script is not None and not item.blocked
                else []
            )
            if not steps:
                result.append(item)
                continue
            if environment.kind == "production":
                result.append(
                    replace(
                        item,
                        blocked="Sensitive steps are never run against a production environment: "
                        + steps[0].reason,
                    )
                )
            elif approved is None:
                result.append(
                    replace(
                        item,
                        blocked="Needs a person's approval before it runs: " + steps[0].reason,
                    )
                )
            else:
                found[item.case.key] = steps
                result.append(item)
        if found and approved is False:
            raise ConflictError(
                "This run contains sensitive steps. Review them and approve them for this "
                "execution, or leave those cases out.",
                code="sensitive_approval_required",
                errors=[
                    ErrorDetail(field=key, code=step.kind, message=step.reason)
                    for key, steps in found.items()
                    for step in steps
                ],
            )
        return result, found

    @staticmethod
    def _changes_state(selected: Sequence[Selected]) -> bool:
        return any(
            changes_state(step) or s.script.login_as is not None
            for s in selected
            if s.script is not None and not s.blocked
            for step in (*s.script.steps, *s.script.cleanup)
        )

    # --- reading -----------------------------------------------------------------------

    async def list_recent(self, ctx: ProjectContext) -> Sequence[TestExecution]:
        ctx.require(Permission.PROJECTS_READ)
        return await ExecutionRepository(self._session, ctx.organization_id).list_in_project(
            ctx.project.id
        )

    async def get(self, ctx: ProjectContext, execution_id: uuid.UUID) -> TestExecution:
        ctx.require(Permission.PROJECTS_READ)
        execution = await ExecutionRepository(self._session, ctx.organization_id).get_in_project(
            ctx.project.id, execution_id
        )
        if execution is None:
            raise NotFoundError("Execution not found.")
        return execution

    async def view(self, ctx: ProjectContext, execution: TestExecution) -> ExecutionView:
        results = await ResultRepository(self._session, ctx.organization_id).for_execution(
            execution.id
        )
        cases = await TestCaseRepository(self._session, ctx.organization_id).by_ids_any(
            [r.test_case_id for r in results]
        )
        versions = await CaseVersionRepository(self._session, ctx.organization_id).by_ids(
            [r.test_case_version_id for r in results]
        )
        return ExecutionView(execution, results, cases, versions)

    async def steps(
        self, ctx: ProjectContext, execution: TestExecution, result_id: uuid.UUID
    ) -> tuple[Sequence[TestExecutionStep], dict[uuid.UUID, list[uuid.UUID]]]:
        result = await ResultRepository(self._session, ctx.organization_id).get_in_execution(
            execution.id, result_id
        )
        if result is None:
            raise NotFoundError("Result not found.")
        steps = await StepRepository(self._session, ctx.organization_id).for_result(result.id)
        by_step: dict[uuid.UUID, list[uuid.UUID]] = {}
        for item in await EvidenceRepository(self._session, ctx.organization_id).for_execution(
            execution.id
        ):
            if item.execution_step_id is not None:
                by_step.setdefault(item.execution_step_id, []).append(item.id)
        return steps, by_step

    async def evidence(
        self, ctx: ProjectContext, execution: TestExecution
    ) -> Sequence[TestEvidence]:
        return await EvidenceRepository(self._session, ctx.organization_id).for_execution(
            execution.id
        )

    # --- cancelling --------------------------------------------------------------------

    async def cancel(self, ctx: ProjectContext, execution_id: uuid.UUID) -> TestExecution:
        ctx.require(Permission.EXECUTIONS_RUN)
        executions = ExecutionRepository(self._session, ctx.organization_id)
        execution = await executions.get_in_project(ctx.project.id, execution_id, for_update=True)
        if execution is None:
            raise NotFoundError("Execution not found.")
        if execution.status in ENDED:
            raise ConflictError("This execution has already ended.", code="execution_ended")
        now = datetime.now(UTC)
        execution.cancel_requested_at = now
        execution.cancelled_by = ctx.user.id
        if execution.status == "queued":
            # The worker has not started; it will find the execution cancelled and leave it.
            results = await ResultRepository(self._session, ctx.organization_id).for_execution(
                execution.id
            )
            _conclude(execution, results, status="cancelled")
        else:
            execution.status = "cancelling"
            await self._redis.set(
                cancel_key(execution.id), "1", ex=self._settings.execution_timeout_s + 600
            )
        await self._session.flush()
        await self._audit.record(
            self._session,
            AuditEvent(
                action="execution.cancel",
                actor_id=ctx.user.id,
                organization_id=ctx.organization_id,
                resource_type="test_execution",
                resource_id=execution.id,
                changes={},
            ),
        )
        return execution


def _conclude(
    execution: TestExecution,
    results: Sequence[TestResult],
    *,
    status: str = "completed",
    error: Mapping[str, Any] | None = None,
) -> None:
    """Close an execution: anything that did not finish is marked as not run."""
    now = datetime.now(UTC)
    for result in results:
        if result.status in ("pending", "running"):
            result.status = "skipped"
            result.failure_summary = (
                result.failure_summary or "The execution ended before this case ran."
            )
            result.finished_at = result.finished_at or now
    for result in results:
        if result.failure_category is None:
            category = classify(
                status=result.status,
                error_type=None,
                failure_class=result.failure_class,
                details=result.details,
                execution_error=(error or {}).get("code"),
            )
            if category is not None:
                result.failure_category = category.category
                result.details = {**result.details, "classification": category.as_dict()}
    statuses = [result.status for result in results]
    execution.status = status
    execution.verdict = execution_verdict(statuses)
    execution.totals = totals(statuses)
    execution.finished_at = now


ACTIVE_EXECUTIONS = "executions:active"


def alive_key(execution_id: uuid.UUID | str) -> str:
    return f"execution:alive:{execution_id}"


async def _record_metrics(execution: TestExecution, results: Sequence[TestResult]) -> None:
    samples: dict[str, float] = {
        f'qa_executions_total{{status="{execution.status}",verdict="{execution.verdict}"}}': 1
    }
    if execution.started_at and execution.finished_at:
        seconds = (execution.finished_at - execution.started_at).total_seconds()
        samples["qa_execution_duration_seconds_sum"] = max(seconds, 0)
        samples["qa_execution_duration_seconds_count"] = 1
    if execution.started_at and execution.queued_at:
        delay = (execution.started_at - execution.queued_at).total_seconds()
        samples["qa_execution_queue_delay_seconds_sum"] = max(delay, 0)
        samples["qa_execution_queue_delay_seconds_count"] = 1
    for result in results:
        name = f'qa_test_results_total{{status="{result.status}"}}'
        samples[name] = samples.get(name, 0) + 1
    await metrics.add(samples)


async def recover_stale_executions(runner: "ExecutionRunner", redis: RedisClient) -> list[str]:
    """Close executions whose worker stopped. Returns the IDs that were closed.

    An execution a worker is running is listed in a Redis set and kept alive with
    a key that expires. Listed without that key means the worker is gone. Nothing
    is run again: what a half-finished test did is unknown, so the case in
    progress becomes an error and the rest are marked as not run.
    """
    closed: list[str] = []
    members: Any = await redis.smembers(ACTIVE_EXECUTIONS)  # type: ignore[misc]
    for raw in members:
        member = raw.decode() if isinstance(raw, bytes) else str(raw)
        organization, _, execution = member.partition(":")
        try:
            organization_id, execution_id = uuid.UUID(organization), uuid.UUID(execution)
        except ValueError:
            await redis.srem(ACTIVE_EXECUTIONS, raw)  # type: ignore[misc]
            continue
        if await redis.exists(alive_key(execution_id)):
            continue
        await redis.srem(ACTIVE_EXECUTIONS, raw)  # type: ignore[misc]
        if await runner.is_part_of_workflow(organization_id, execution_id):
            continue  # the workflow's own recovery closes it and carries on
        outcome = await runner.conclude_interrupted(organization_id, execution_id)
        logger.warning(
            "execution_recovered", extra={"execution_id": str(execution_id), "outcome": outcome}
        )
        await metrics.inc("qa_jobs_recovered_total", {"kind": "execution"})
        closed.append(str(execution_id))
    return closed


# --- worker side -------------------------------------------------------------------------


@dataclass(slots=True)
class _Prepared:
    plan: RunPlan
    destinations: DestinationPolicy
    proxy_secret: str | None


@dataclass(slots=True)
class _CaseBuffer:
    """Step events of the case attempt in progress, written together when it ends."""

    steps: list[StepFinished] = field(default_factory=list)


class ExecutionRunner:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        redis: RedisClient,
        cipher: CredentialCipher,
        engine: EngineClient | None,
        egress: EgressClient | None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._redis = redis
        self._cipher = cipher
        self._engine = engine
        self._egress = egress
        self._store = EvidenceStore(settings.evidence_root)
        self._limits = EvidenceLimits.from_settings(settings)
        # Per execution in progress: bytes of evidence kept so far, and how logs are captured.
        self._stored_bytes: dict[uuid.UUID, int] = {}
        self._log_modes: dict[uuid.UUID, str] = {}

    async def run(self, *, organization_id: uuid.UUID, execution_id: uuid.UUID) -> str:
        prepared = await self._prepare(organization_id, execution_id)
        if prepared is None:
            return "discarded"
        outcome: tuple[str, dict[str, str] | None] = ("failed", None)
        registered = False
        # Listed as active and kept alive while this worker is on it. If the worker
        # dies none of the clean-up below runs: the entry stays, its sign of life
        # expires, and `recover_stale_executions` closes the execution.
        member = f"{organization_id}:{execution_id}"
        await self._redis.sadd(ACTIVE_EXECUTIONS, member)  # type: ignore[misc]
        alive = asyncio.create_task(self._keep_alive(execution_id))
        try:
            if self._engine is None:
                raise EngineError("engine_not_configured", "The test engine is not configured.")
            if prepared.proxy_secret is not None:
                assert self._egress is not None  # noqa: S101 - a secret implies a client
                await self._egress.register(
                    execution_id,
                    prepared.proxy_secret,
                    prepared.destinations,
                    ttl_s=self._settings.execution_timeout_s + 300,
                )
                registered = True
            outcome = await self._drive(organization_id, execution_id, prepared.plan)
        except EngineError as exc:
            outcome = ("failed", {"code": exc.code, "message": exc.message})
        except BrowserError as exc:
            outcome = ("failed", {"code": exc.code, "message": exc.message})
        except Exception:
            logger.exception("execution_failed", extra={"execution_id": str(execution_id)})
            outcome = (
                "failed",
                {"code": "internal_error", "message": "The execution failed unexpectedly."},
            )
        finally:
            blocked: list[dict[str, Any]] = []
            if self._engine is not None:
                await self._engine.delete_run(str(execution_id))
            if registered and self._egress is not None:
                with contextlib.suppress(BrowserError):
                    blocked = await self._egress.unregister(execution_id)
            status = await asyncio.shield(
                self._finish(organization_id, execution_id, outcome[0], outcome[1], blocked)
            )
            alive.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await alive
            await self._redis.delete(cancel_key(execution_id), alive_key(execution_id))
            await self._redis.srem(ACTIVE_EXECUTIONS, member)  # type: ignore[misc]
        return status

    async def _keep_alive(self, execution_id: uuid.UUID) -> None:
        ttl = self._settings.execution_stale_after_s
        while True:
            await self._redis.set(alive_key(execution_id), "1", ex=ttl)
            await asyncio.sleep(ttl / 4)

    async def is_part_of_workflow(
        self, organization_id: uuid.UUID, execution_id: uuid.UUID
    ) -> bool:
        async with self._session_factory() as session, session.begin():
            await set_current_org(session, organization_id)
            execution = await ExecutionRepository(session, organization_id).get(execution_id)
            return execution is not None and execution.graph_thread_id is not None

    async def conclude_interrupted(
        self, organization_id: uuid.UUID, execution_id: uuid.UUID
    ) -> str:
        """Close an execution whose worker stopped while it was running.

        Cases that were in progress become errors and the rest are marked as not
        run. Nothing is executed again: what a half-finished case did is unknown.
        """
        if self._engine is not None:
            await self._engine.delete_run(str(execution_id))
        if self._egress is not None:
            with contextlib.suppress(BrowserError):
                await self._egress.unregister(execution_id)
        return await self._finish(
            organization_id,
            execution_id,
            "failed",
            {
                "code": "worker_interrupted",
                "message": "The worker stopped while this execution was running.",
            },
            [],
        )

    async def close_unstarted(
        self, organization_id: uuid.UUID, execution_id: uuid.UUID, *, cancelled: bool
    ) -> str:
        """Close an execution that was prepared and will not be run."""
        return await self._finish(
            organization_id,
            execution_id,
            "cancelled" if cancelled else "failed",
            None
            if cancelled
            else {
                "code": "workflow_ended",
                "message": "The workflow ended before this execution ran.",
            },
            [],
        )

    # --- preparation ---------------------------------------------------------------------

    async def _prepare(
        self, organization_id: uuid.UUID, execution_id: uuid.UUID
    ) -> _Prepared | None:
        """Claim the execution and build the plan, in one transaction."""
        async with self._session_factory() as session, session.begin():
            await set_current_org(session, organization_id)
            execution = await ExecutionRepository(session, organization_id).get_for_update(
                execution_id
            )
            if execution is None or execution.status != "queued":
                return None
            environment = await EnvironmentRepository(session, organization_id).get(
                execution.environment_id
            )
            results = await ResultRepository(session, organization_id).for_execution(execution.id)
            pending = [r for r in results if r.status == "pending"]
            if environment is None or environment.status != "active" or not pending:
                execution.error = {
                    "code": "environment_gone",
                    "message": "The environment is no longer available.",
                }
                _conclude(execution, results, status="failed")
                return None
            facts = await load_environment_facts(
                session,
                organization_id=organization_id,
                project_id=execution.project_id,
                environment=environment,
            )
            cases = await TestCaseRepository(session, organization_id).by_ids_any(
                [r.test_case_id for r in pending]
            )
            selected = await select_cases(
                session,
                organization_id,
                facts,
                [(cases[r.test_case_id], r.test_case_version_id) for r in pending],
            )
            planned: list[PlannedCase] = []
            for result, item in zip(pending, selected, strict=True):
                if item.script is None or item.blocked:
                    # Something changed since the execution was requested.
                    result.status = "blocked"
                    result.failure_class = "test_data"
                    result.failure_summary = item.blocked or item.skipped
                    continue
                planned.append(
                    PlannedCase(
                        result_id=str(result.id),
                        key=item.case.key,
                        title=item.version.title,
                        script=item.script,
                        data=facts.data_for(item.script, item.content),
                    )
                )
            if not planned:
                _conclude(execution, results)
                return None
            config = RunConfig.model_validate(
                {k: v for k, v in execution.config.items() if k in RunConfig.model_fields}
            )
            self._log_modes[execution.id] = config.evidence.logs
            proxy_secret = secrets.token_urlsafe(24) if self._egress is not None else None
            plan = RunPlan(
                run_id=str(execution.id),
                config=config,
                base_url=environment.base_url,
                allowed_origins=sorted(str(origin) for origin in facts.origins),
                allow_http=environment.kind != "production",
                read_only=environment.is_read_only,
                allowed_actions=self._allowed_actions(environment, execution),
                proxy=self._proxy(execution.id, proxy_secret),
                env=facts.variables,
                credentials=await self._credentials(session, execution, facts, planned),
                files=await self._files(session, organization_id, execution.project_id, planned),
                cases=planned,
            )
            execution.status = "running"
            execution.started_at = datetime.now(UTC)
            return _Prepared(
                plan,
                DestinationPolicy(
                    allowed_origins=facts.origins,
                    allow_private_network=environment.allow_private_network,
                    allow_http=environment.kind != "production",
                ),
                proxy_secret,
            )

    @staticmethod
    def _allowed_actions(environment: Environment, execution: TestExecution) -> list[str]:
        """Without the recorded approval, an approval-required environment is looked at only."""
        actions = set(environment.allowed_actions)
        if environment.requires_approval and not execution.config.get("state_changes_approved"):
            actions &= {"navigate", "read", "api_read"}
        return sorted(actions)

    def _proxy(self, execution_id: uuid.UUID, secret: str | None) -> ProxySettings | None:
        if secret is None or self._egress is None:
            return None
        url = self._egress.proxy_url_for(execution_id, secret)
        scheme, _, rest = url.partition("://")
        return ProxySettings(
            server=f"{scheme}://{rest.rsplit('@', 1)[-1]}",
            username=str(execution_id),
            password=secret,
        )

    async def _credentials(
        self,
        session: AsyncSession,
        execution: TestExecution,
        facts: EnvironmentFacts,
        planned: Sequence[PlannedCase],
    ) -> dict[str, CredentialGrant]:
        """Decrypt only the credentials these cases refer to. Each use is audited."""
        roles: set[str] = set()
        for case in planned:
            if case.script.login_as:
                roles.add(case.script.login_as)
            for namespace, name in placeholders_in([*case.script.steps, *case.script.cleanup]):
                if namespace == "cred":
                    roles.add(name.partition(".")[0])
        service = CredentialService(
            session=session, audit=AuditService(self._session_factory), cipher=self._cipher
        )
        grants: dict[str, CredentialGrant] = {}
        for role in sorted(roles):
            credential = facts.credentials.get(role)
            if credential is None or credential.source != "stored":
                continue  # the script check has already blocked cases that need it
            fields = await service.reveal_for_job(
                credential,
                requested_by=execution.triggered_by,
                purpose="execution",
                reference_id=execution.id,
            )
            grants[role] = CredentialGrant(
                fields=fields, login_recipe=list(credential.login_recipe)
            )
        return grants

    @staticmethod
    async def _files(
        session: AsyncSession,
        organization_id: uuid.UUID,
        project_id: uuid.UUID,
        planned: Sequence[PlannedCase],
    ) -> list[FileGrant]:
        names = sorted(
            {
                step.file
                for case in planned
                for step in (*case.script.steps, *case.script.cleanup)
                if step.action == "upload_file"
            }
        )
        files = await TestFileRepository(session, organization_id).with_content(project_id, names)
        return [
            FileGrant(
                name=f.name,
                content_type=f.content_type,
                content_base64=base64.b64encode(f.content).decode(),
            )
            for f in files
        ]

    # --- running -------------------------------------------------------------------------

    async def _drive(
        self, organization_id: uuid.UUID, execution_id: uuid.UUID, plan: RunPlan
    ) -> tuple[str, dict[str, str] | None]:
        """Consume the engine's events. Returns the execution's end status and error."""
        assert self._engine is not None  # noqa: S101
        consume = asyncio.create_task(self._consume(organization_id, execution_id, plan))
        watch = asyncio.create_task(self._watch_for_cancel(execution_id))
        try:
            done, _ = await asyncio.wait(
                {consume, watch},
                timeout=self._settings.execution_timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (consume, watch):
                if not task.done():
                    # Ending the consumer closes the stream, which stops the run in the engine.
                    task.cancel()
            await asyncio.gather(consume, watch, return_exceptions=True)
        if consume in done:
            finished = consume.result()
            if finished.status == "completed":
                return "completed", None
            if finished.error_type == "run_timeout":
                return "timed_out", {"code": "run_timeout", "message": finished.error_message or ""}
            return "failed", {
                "code": finished.error_type or "engine_aborted",
                "message": finished.error_message or "The test engine stopped the run.",
            }
        if watch in done:
            return "cancelled", None
        return "timed_out", {
            "code": "execution_timeout",
            "message": f"The execution did not finish within {self._settings.execution_timeout_s} seconds.",
        }

    async def _watch_for_cancel(self, execution_id: uuid.UUID) -> None:
        # The request to cancel arrives in another process (the API), through Redis.
        while not await self._redis.exists(cancel_key(execution_id)):  # noqa: ASYNC110
            await asyncio.sleep(1.0)

    async def _consume(
        self, organization_id: uuid.UUID, execution_id: uuid.UUID, plan: RunPlan
    ) -> RunFinished:
        assert self._engine is not None  # noqa: S101
        buffers: dict[tuple[str, int], _CaseBuffer] = {}
        # For people watching: what the engine reports, as it reports it.
        progress = ProgressWriter(self._redis, execution_id)
        # The engine removes secrets from what it sends. They are removed here again,
        # by the side that handed them out, so that nothing stored depends on the
        # engine having done so.
        redactor = Redactor(
            value for grant in plan.credentials.values() for value in grant.fields.values()
        )
        try:
            async for raw_event in self._engine.run(plan):
                event: Any = raw_event
                if isinstance(raw_event, StepFinished | CaseFinished):
                    event = type(raw_event).model_validate(
                        redactor.value(raw_event.model_dump(mode="json"))
                    )
                if isinstance(event, CaseStarted):
                    buffers[(event.result_id, event.attempt)] = _CaseBuffer()
                    await self._case_started(organization_id, event)
                    await progress.case_started(event)
                elif isinstance(event, StepFinished):
                    buffers.setdefault(
                        (event.result_id, event.attempt), _CaseBuffer()
                    ).steps.append(event)
                    await progress.step_finished(event)
                elif isinstance(event, CaseFinished):
                    buffer = buffers.pop((event.result_id, event.attempt), _CaseBuffer())
                    await self._case_finished(organization_id, execution_id, event, buffer.steps)
                    await progress.case_finished(event)
                elif isinstance(event, RunFinished):
                    return event
        finally:
            await progress.ended()
        return RunFinished(
            status="aborted",
            error_type="engine_protocol",
            error_message="The engine's answer ended early.",
        )

    async def _case_started(self, organization_id: uuid.UUID, event: CaseStarted) -> None:
        async with self._session_factory() as session, session.begin():
            await set_current_org(session, organization_id)
            result = await ResultRepository(session, organization_id).get(
                uuid.UUID(event.result_id)
            )
            if result is None:
                return
            result.status = "running"
            result.attempt_count = event.attempt
            result.started_at = result.started_at or event.started_at

    async def _case_finished(
        self,
        organization_id: uuid.UUID,
        execution_id: uuid.UUID,
        event: CaseFinished,
        steps: Sequence[StepFinished],
    ) -> None:
        assert self._engine is not None  # noqa: S101
        # Fetch and check evidence first, outside any transaction.
        files: dict[str, tuple[Any, Any]] = {}
        notes = list(event.details.get("evidence_notes", []))
        for artifact in event.artifacts:
            try:
                data = await self._engine.fetch_artifact(str(execution_id), artifact.name)
            except EngineError as exc:
                notes.append(f"{artifact.kind}_not_stored:{exc.code}")
                continue
            stored = self._keep(
                organization_id, execution_id, artifact.kind, artifact.content_type, data, notes
            )
            if stored is not None:
                files[artifact.name] = (artifact, stored)
        details = dict(event.details)
        logs = self._logs(organization_id, execution_id, event, details, notes)

        async with self._session_factory() as session, session.begin():
            await set_current_org(session, organization_id)
            result = await ResultRepository(session, organization_id).get(
                uuid.UUID(event.result_id)
            )
            if result is None or result.execution_id != execution_id:
                return
            step_ids: dict[str, uuid.UUID] = {}
            for step in steps:
                row = TestExecutionStep(
                    organization_id=organization_id,
                    execution_id=execution_id,
                    result_id=result.id,
                    attempt=step.attempt,
                    position=step.position,
                    phase=step.phase,
                    action=step.action,
                    description=step.description,
                    status=step.status,
                    page_url=step.page_url,
                    resolved_locator=step.locator,
                    expected=step.expected,
                    actual=step.actual,
                    error_type=step.error_type,
                    error_message=step.error_message,
                    script_index=step.script_index,
                    healed_from=(
                        step.healing.get("original")
                        if step.healing and step.healing.get("verified")
                        else None
                    ),
                    started_at=step.started_at,
                    finished_at=step.finished_at,
                    duration_ms=step.duration_ms,
                )
                session.add(row)
                await session.flush()
                if step.artifact is not None:
                    step_ids[step.artifact.name] = row.id
                if step.healing and step.healing.get("replacement"):
                    await self._record_heal(session, result, row, step.healing)
            for name, (artifact, stored) in files.items():
                session.add(
                    TestEvidence(
                        organization_id=organization_id,
                        execution_id=execution_id,
                        result_id=result.id,
                        execution_step_id=step_ids.get(name),
                        kind=artifact.kind,
                        storage_key=stored.key,
                        content_type=artifact.content_type,
                        size_bytes=stored.size_bytes,
                        sha256=stored.sha256,
                        viewport=result.viewport,
                        captured_at=event.finished_at,
                        expires_at=self._limits.expires_at(artifact.kind),
                    )
                )
            for kind, stored in logs:
                session.add(
                    TestEvidence(
                        organization_id=organization_id,
                        execution_id=execution_id,
                        result_id=result.id,
                        kind=kind,
                        storage_key=stored.key,
                        content_type=KINDS[kind][0],
                        size_bytes=stored.size_bytes,
                        sha256=stored.sha256,
                        # Text passed the engine's redactor and the shape rules here.
                        redaction_status="applied",
                        captured_at=event.finished_at,
                        expires_at=self._limits.expires_at(kind),
                    )
                )
            result.attempt_count = event.attempt
            if event.final:
                healed = int(event.details.get("healed_steps", 0) or 0)
                result.status = event.status
                result.failure_class = event.failure_class
                result.failure_summary = event.failure_summary
                if healed and event.status in ("passed", "flaky"):
                    # Every step passed, but not as the approved script was written.
                    # That is not a pass until a person has looked at the replacements.
                    result.status = "needs_review"
                    result.failure_summary = (
                        f"Passed only after {healed} locator(s) were replaced during the run. "
                        "Review the proposed replacements before relying on this result."
                    )
                result.healed = healed > 0
                final_steps = [
                    step.model_dump(mode="json") for step in steps if step.attempt == event.attempt
                ]
                stopped = next(
                    (
                        step
                        for step in final_steps
                        if step["status"] in ("failed", "error") and step["phase"] != "cleanup"
                    ),
                    None,
                )
                category = classify(
                    status=result.status,
                    error_type=stopped["error_type"] if stopped else None,
                    failure_class=result.failure_class,
                    details=details,
                    actual=stopped["actual"] if stopped else None,
                )
                result.failure_category = category.category if category else None
                result.finished_at = event.finished_at
                result.duration_ms = int(
                    (event.finished_at - (result.started_at or event.started_at)).total_seconds()
                    * 1000
                )
                result.details = {
                    **details,
                    "evidence_notes": notes,
                    "verification": verification_summary(final_steps),
                    **({"classification": category.as_dict()} if category else {}),
                }

    def _keep(
        self,
        organization_id: uuid.UUID,
        execution_id: uuid.UUID,
        kind: str,
        content_type: str,
        data: bytes,
        notes: list[str],
    ) -> StoredFile | None:
        """Store one evidence file if it is valid and fits. Otherwise note why not."""
        used = self._stored_bytes.get(execution_id, 0)
        try:
            validate(kind, content_type, data, self._limits)
            if used + len(data) > self._limits.execution_bytes:
                raise EvidenceRejectedError("execution_quota", "The execution's evidence is full.")
        except EvidenceRejectedError as exc:
            notes.append(f"{kind}_not_stored:{exc.code}")
            return None
        self._stored_bytes[execution_id] = used + len(data)
        return self._store.put(organization_id, execution_id, content_type, data)

    def _logs(
        self,
        organization_id: uuid.UUID,
        execution_id: uuid.UUID,
        event: CaseFinished,
        details: dict[str, Any],
        notes: list[str],
    ) -> list[tuple[str, StoredFile]]:
        """Console and network logs of one attempt, as files. Removes the full log from `details`."""
        console = details.pop("console_log", None) or []
        mode = self._log_modes.get(execution_id, "on_failure")
        if mode == "off" or (mode == "on_failure" and event.status == "passed"):
            return []
        redact = Redactor()
        documents = {
            "console_log": {
                "attempt": event.attempt,
                "messages": redact.value(console),
                "page_errors": redact.value(details.get("page_errors", [])),
            },
            "network_log": {
                "attempt": event.attempt,
                "failed_requests": redact.value(details.get("failed_requests", [])),
                "blocked_requests": redact.value(details.get("blocked_requests", [])),
            },
        }
        kept: list[tuple[str, StoredFile]] = []
        for kind, document in documents.items():
            if not any(value for key, value in document.items() if key != "attempt"):
                continue  # nothing was reported: no empty file
            data = json.dumps(document, ensure_ascii=False, indent=1).encode()
            stored = self._keep(organization_id, execution_id, kind, KINDS[kind][0], data, notes)
            if stored is not None:
                kept.append((kind, stored))
        return kept

    async def _record_heal(
        self,
        session: AsyncSession,
        result: TestResult,
        step: TestExecutionStep,
        healing: dict[str, Any],
    ) -> None:
        """Keep a replacement locator the engine found, for a person to decide on.

        Recording it changes nothing about the test case.
        """
        proposals = HealProposalRepository(session, result.organization_id)
        if await proposals.open_duplicate(
            version_id=result.test_case_version_id,
            phase=step.phase,
            script_index=step.script_index,
            new_locator=healing["replacement"],
        ):
            return
        session.add(
            LocatorHealProposal(
                organization_id=result.organization_id,
                test_case_id=result.test_case_id,
                test_case_version_id=result.test_case_version_id,
                result_id=result.id,
                execution_step_id=step.id,
                script_phase=step.phase,
                script_index=step.script_index,
                action=step.action,
                old_locator=healing.get("original") or {},
                new_locator=healing["replacement"],
                confidence=Decimal(str(healing.get("confidence", 0))).quantize(Decimal("0.01")),
                rationale=healing.get("rationale"),
                held_back=healing.get("held_back"),
                status="verified" if healing.get("verified") else "proposed",
                verified_by_rerun=bool(healing.get("verified")),
            )
        )
        await session.flush()

    # --- finishing -----------------------------------------------------------------------

    async def _finish(
        self,
        organization_id: uuid.UUID,
        execution_id: uuid.UUID,
        status: str,
        error: dict[str, str] | None,
        blocked: list[dict[str, Any]],
    ) -> str:
        async with self._session_factory() as session, session.begin():
            await set_current_org(session, organization_id)
            execution = await ExecutionRepository(session, organization_id).get_for_update(
                execution_id
            )
            if execution is None:
                return "discarded"
            results = await ResultRepository(session, organization_id).for_execution(execution.id)
            if status != "completed":
                reason = {
                    "cancelled": "The execution was cancelled.",
                    "timed_out": "The execution ran out of time.",
                }.get(status, "The test engine stopped before this case finished.")
                for result in results:
                    if result.status == "running":
                        # Started and never finished: not known to be right or wrong.
                        result.status = "error"
                        result.failure_class = "infrastructure"
                        result.failure_summary = reason
                        result.finished_at = datetime.now(UTC)
            if (
                execution.status not in ACTIVE_EXECUTION_STATUSES
                and execution.status != "cancelling"
            ):
                # Already closed (for example by recovery while this worker was stalled).
                return f"{execution.status}:{execution.verdict}"
            _conclude(execution, results, status=status, error=error)
            execution.error = error
            if blocked:
                execution.totals = {**execution.totals, "blocked_requests": blocked[:50]}
            await _record_metrics(execution, results)
            return f"{execution.status}:{execution.verdict}"
