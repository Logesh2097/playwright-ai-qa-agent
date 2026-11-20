"""The agent workflow, from the API's side.

The API never runs the graph. It records what a person asked for or decided,
checks they are allowed to, and hands the run to the worker
(`app.workflow.runner`).
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.llm.base import LLMGateway
from app.core.config import Settings
from app.core.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    RateLimitedError,
    ServiceUnavailableError,
)
from app.core.jobs import JobQueue
from app.core.redis import RedisClient
from app.core.security.permissions import Permission, has_permission
from app.models import WorkflowRun
from app.models.workflow import ENDED_WORKFLOW_STATUSES
from app.repositories.instructions import InstructionRepository
from app.repositories.projects import ApplicationRepository, EnvironmentRepository
from app.repositories.test_cases import SuiteRepository
from app.repositories.workflows import (
    ApprovalRepository,
    WorkflowLogRepository,
    WorkflowRunRepository,
)
from app.schemas.instructions import ClarificationAnswer, InstructionCreate
from app.schemas.workflows import WorkflowCreate
from app.services.audit import AuditEvent, AuditService
from app.services.executions import cancel_key
from app.services.instructions import InstructionService
from app.services.projects import ProjectContext
from app.workflow.deps import CaptureQueue
from app.workflow.observability import summarize
from app.workflow.runner import JOB_NAME, lock_key


class WorkflowService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        audit: AuditService,
        jobs: JobQueue,
        redis: RedisClient,
        settings: Settings,
        gateway: LLMGateway,
    ) -> None:
        self._session = session
        self._audit = audit
        self._jobs = jobs
        self._redis = redis
        self._settings = settings
        self._gateway = gateway

    def _runs(self, ctx: ProjectContext) -> WorkflowRunRepository:
        return WorkflowRunRepository(self._session, ctx.organization_id)

    # --- starting ----------------------------------------------------------------------

    async def start(self, ctx: ProjectContext, data: WorkflowCreate) -> WorkflowRun:
        ctx.require(Permission.EXECUTIONS_RUN)
        ctx.require_writable()
        if not self._settings.engine_configured:
            raise ServiceUnavailableError(
                "The test engine is not configured on this installation.",
                code="engine_not_configured",
            )
        if await self._runs(ctx).count_open() >= self._settings.workflow_max_open:
            raise RateLimitedError(
                "Too many workflow runs are in progress or waiting for a decision.",
                code="too_many_workflows",
            )
        environment = await EnvironmentRepository(self._session, ctx.organization_id).get(
            data.environment_id
        )
        application = (
            await ApplicationRepository(self._session, ctx.organization_id).get(
                environment.application_id
            )
            if environment
            else None
        )
        if environment is None or application is None or application.project_id != ctx.project.id:
            raise NotFoundError("Environment not found.")

        instruction_id: uuid.UUID | None = None
        if data.text is not None:
            if not self._gateway.configured:
                raise ServiceUnavailableError(
                    "The AI service is not configured on this installation.",
                    code="llm_not_configured",
                )
            assert data.application_id is not None  # noqa: S101 - checked by the schema
            instruction = await InstructionService(
                session=self._session,
                audit=self._audit,
                # Nothing is enqueued here: the workflow does the analysis itself.
                jobs=CaptureQueue(),
                gateway=self._gateway,
                redis=self._redis,
                settings=self._settings,
            ).submit(
                ctx,
                InstructionCreate(
                    application_id=data.application_id,
                    environment_id=data.environment_id,
                    text=data.text,
                    test_types=data.test_types,
                    options=data.rules,
                ),
                analyse=False,
            )
            instruction_id = instruction.id
        else:
            assert data.suite_id is not None  # noqa: S101 - checked by the schema
            suite = await SuiteRepository(self._session, ctx.organization_id).get_in_project(
                ctx.project.id, data.suite_id
            )
            if suite is None:
                raise NotFoundError("Test suite not found.")
            if suite.status != "approved":
                raise ConflictError("Only an approved suite can be run.", code="suite_not_approved")

        run = await self._runs(ctx).add(
            WorkflowRun(
                organization_id=ctx.organization_id,
                project_id=ctx.project.id,
                environment_id=environment.id,
                instruction_id=instruction_id,
                suite_id=data.suite_id,
                mode="generate_and_run" if data.text is not None else "run_existing",
                status="queued",
                started_by=ctx.user.id,
                options=data.options.model_dump(mode="json"),
            )
        )
        if instruction_id is not None:
            stored = await InstructionRepository(self._session, ctx.organization_id).get(
                instruction_id
            )
            if stored is not None:
                stored.graph_thread_id = str(run.id)
        await self._record(
            ctx, "workflow.start", run, {"mode": run.mode, "environment_id": str(environment.id)}
        )
        await self._enqueue(run)
        return run

    async def _enqueue(self, run: WorkflowRun, resume: dict[str, Any] | None = None) -> None:
        await self._jobs.enqueue(
            JOB_NAME, organization_id=str(run.organization_id), run_id=str(run.id), resume=resume
        )

    # --- reading -----------------------------------------------------------------------

    async def list_runs(self, ctx: ProjectContext) -> Sequence[WorkflowRun]:
        ctx.require(Permission.PROJECTS_READ)
        return await self._runs(ctx).list_in_project(ctx.project.id)

    async def get(
        self, ctx: ProjectContext, run_id: uuid.UUID, *, for_update: bool = False
    ) -> WorkflowRun:
        ctx.require(Permission.PROJECTS_READ)
        run = await self._runs(ctx).get_in_project(ctx.project.id, run_id, for_update=for_update)
        if run is None:
            raise NotFoundError("Workflow run not found.")
        return run

    async def trace(
        self, ctx: ProjectContext, run: WorkflowRun
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Totals (model calls, tokens, tool calls) and the node history of a run."""
        logs = await WorkflowLogRepository(self._session, ctx.organization_id).for_run(
            run.id, run.instruction_id
        )
        usage, nodes = summarize(logs)
        usage["retries"] = run.retry_count
        return usage, nodes

    # --- a person's part ---------------------------------------------------------------

    async def clarify(
        self, ctx: ProjectContext, run_id: uuid.UUID, answers: Sequence[ClarificationAnswer]
    ) -> WorkflowRun:
        run = await self.get(ctx, run_id, for_update=True)
        if run.status != "waiting_for_clarification" or run.instruction_id is None:
            raise ConflictError(
                "This run is not waiting for answers.", code="invalid_state_transition"
            )
        instruction = await InstructionRepository(
            self._session, ctx.organization_id
        ).get_for_update(run.instruction_id)
        if instruction is None:
            raise NotFoundError("Instruction not found.")
        # Validates and stores the answers. The analysis itself is run by the workflow.
        await InstructionService(
            session=self._session,
            audit=self._audit,
            jobs=CaptureQueue(),
            gateway=self._gateway,
            redis=self._redis,
            settings=self._settings,
        ).clarify(ctx, instruction, answers)
        run.status = "queued"
        run.pending = None
        await self._session.flush()
        await self._enqueue(run, {"type": "clarification", "answered_by": str(ctx.user.id)})
        return run

    async def decide(
        self, ctx: ProjectContext, run_id: uuid.UUID, *, approve: bool, note: str | None
    ) -> WorkflowRun:
        """Record an approval or a rejection. Only then does the run continue."""
        ctx.require(Permission.CASES_APPROVE)
        ctx.require_writable()
        run = await self.get(ctx, run_id, for_update=True)
        approval = await ApprovalRepository(self._session, ctx.organization_id).latest_for(run.id)
        if run.status != "waiting_for_approval" or approval is None or approval.status != "pending":
            raise ConflictError(
                "This run is not waiting for approval.", code="invalid_state_transition"
            )
        now = datetime.now(UTC)
        if approval.expires_at is not None and approval.expires_at < now:
            approval.status = "expired"
            run.status = "cancelled"
            run.pending = None
            run.finished_at = now
            await self._session.flush()
            raise ConflictError(
                "The approval request has expired. Start a new run.", code="approval_expired"
            )
        if approval.payload.get("needs_admin") and not has_permission(
            ctx.org.role, Permission.ENVIRONMENTS_SECURITY
        ):
            raise ForbiddenError(
                "This run changes data on an environment that requires approval, or targets "
                "production. Only an organization admin can decide it.",
                code="admin_required",
            )
        approval.status = "approved" if approve else "rejected"
        approval.decided_by = ctx.user.id
        approval.decided_at = now
        approval.decision_note = note
        run.status = "queued"
        run.pending = None
        await self._session.flush()
        await self._record(
            ctx,
            "workflow.approve" if approve else "workflow.reject",
            run,
            {"approval_id": str(approval.id), "reasons": approval.payload.get("reasons", [])},
        )
        await self._enqueue(
            run,
            {
                "type": "approval",
                "decision": approval.status,
                "decided_by": str(ctx.user.id),
                "note": note,
            },
        )
        return run

    async def cancel(self, ctx: ProjectContext, run_id: uuid.UUID) -> WorkflowRun:
        ctx.require(Permission.EXECUTIONS_RUN)
        run = await self.get(ctx, run_id, for_update=True)
        if run.status in ENDED_WORKFLOW_STATUSES:
            raise ConflictError("This run has already ended.", code="workflow_ended")
        now = datetime.now(UTC)
        working = await self._redis.exists(lock_key(run.id))
        if working:
            # A worker is on it: it stops at the next node, and the engine is told to stop now.
            run.status = "cancelling"
            if run.execution_ids:
                await self._redis.set(
                    cancel_key(uuid.UUID(str(run.execution_ids[-1]))),
                    "1",
                    ex=self._settings.execution_timeout_s + 600,
                )
        else:
            run.status = "cancelled"
            run.pending = None
            run.finished_at = now
            approval = await ApprovalRepository(self._session, ctx.organization_id).latest_for(
                run.id
            )
            if approval is not None and approval.status == "pending":
                approval.status = "expired"
        await self._session.flush()
        await self._record(ctx, "workflow.cancel", run, {})
        return run

    async def recover(self, ctx: ProjectContext, run_id: uuid.UUID) -> WorkflowRun:
        """Continue a run whose worker stopped, from its last checkpoint."""
        ctx.require(Permission.EXECUTIONS_RUN)
        run = await self.get(ctx, run_id, for_update=True)
        if run.status not in ("queued", "running", "cancelling"):
            raise ConflictError(
                "Only a run that was interrupted while working can be continued.",
                code="invalid_state_transition",
            )
        if await self._redis.exists(lock_key(run.id)):
            raise ConflictError("A worker is still working on this run.", code="workflow_active")
        await self._record(ctx, "workflow.recover", run, {"node": run.current_node})
        await self._enqueue(run)
        return run

    async def _record(
        self, ctx: ProjectContext, action: str, run: WorkflowRun, changes: dict[str, Any]
    ) -> None:
        await self._audit.record(
            self._session,
            AuditEvent(
                action=action,
                actor_id=ctx.user.id,
                organization_id=ctx.organization_id,
                resource_type="workflow_run",
                resource_id=run.id,
                changes=changes,
            ),
        )
