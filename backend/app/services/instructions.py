"""Test instructions and their requirement analysis.

`InstructionService` is what the API calls. It never talks to the LLM itself:
it records what should happen and enqueues a job. `AnalysisRunner` is what the
worker runs: it calls the model, validates the answer with deterministic checks
and stores the outcome.
"""

import asyncio
import logging
import secrets
import uuid
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.guard import clean_text
from app.agent.llm.base import LLMError, LLMGateway
from app.agent.nodes.analyse_requirement import (
    PROMPT_NAME,
    AnalysisContext,
    Clarification,
    analyse_requirement,
)
from app.agent.prompts import get_prompt
from app.agent.schemas.requirement import RequirementAnalysis
from app.agent.validation import TargetFacts, ValidationReport, validate_requirement
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
from app.core.rate_limit import RateLimiter
from app.core.redis import RedisClient
from app.core.security.permissions import Permission, has_permission
from app.core.security.url_safety import parse_origin
from app.db.context import set_current_org
from app.models import Application, Environment, TestInstruction, TestPlan
from app.repositories.instructions import (
    AgentLogRepository,
    InstructionRepository,
    PlanRepository,
    UsageTotals,
)
from app.repositories.projects import (
    ApplicationRepository,
    CredentialRepository,
    EnvironmentRepository,
)
from app.schemas.instructions import (
    ClarificationAnswer,
    InstructionCreate,
    InstructionOptions,
    InstructionUpdate,
)
from app.services.audit import AuditEvent, AuditService
from app.services.llm_usage import LLMUsageRecorder, require_budget
from app.services.projects import ProjectContext

logger = logging.getLogger(__name__)

ANALYSE_JOB = "analyse_instruction"
# Once approved, an instruction's text and analysis are fixed: cases are built on them.
_TERMINAL = frozenset({"approved", "generating", "awaiting_approval", "completed", "cancelled"})


# --- shared by the service and the runner -------------------------------------------------


async def load_target(
    session: AsyncSession, instruction: TestInstruction
) -> tuple[Application | None, Environment | None, TargetFacts, AnalysisContext]:
    """Read what is known about the instruction's application and environment.

    Only names and settings are read. Credential secrets are not loaded at all.
    """
    org = instruction.organization_id
    application = await ApplicationRepository(session, org).get(instruction.application_id)
    environment = await EnvironmentRepository(session, org).get(instruction.environment_id)
    credentials = (
        await CredentialRepository(session, org).list_in_environment(environment.id)
        if environment
        else []
    )
    roles = sorted({credential.role_label for credential in credentials})
    variable_names = [str(v.get("name")) for v in (environment.variables if environment else [])]
    options = InstructionOptions.model_validate(instruction.options or {})

    facts = TargetFacts(
        application_name=application.name if application else "(deleted application)",
        # A deleted application or environment is reported the same way as an inactive one.
        application_status=application.status if application else "deleted",
        application_auth_method=application.auth_method if application else "none",
        allowed_domains=list(application.allowed_domains) if application else [],
        environment_name=environment.name if environment else "(deleted environment)",
        environment_status=environment.status if environment else "deleted",
        environment_is_read_only=environment.is_read_only if environment else True,
        environment_allowed_actions=list(environment.allowed_actions) if environment else [],
        credential_roles=roles,
        variable_names=variable_names,
        configured_rule_names=[rule.name for rule in options.rules],
    )
    context = AnalysisContext(
        application={
            "name": application.name,
            "type": application.type,
            "framework": application.framework,
            "sign_in_method": application.auth_method,
            "description": clean_text(application.description or "", max_chars=500) or None,
        }
        if application
        else {},
        environment={
            "name": environment.name,
            "kind": environment.kind,
            "origin": str(parse_origin(environment.base_url)),
            "is_read_only": environment.is_read_only,
            "allowed_actions": list(environment.allowed_actions),
        }
        if environment
        else {},
        available_credential_roles=roles,
        variable_names=variable_names,
        configured_rules=[rule.model_dump(exclude_none=True) for rule in options.rules],
        requested_test_types=list(instruction.test_types),
    )
    return application, environment, facts, context


def apply_validation(
    instruction: TestInstruction, analysis: RequirementAnalysis, facts: TargetFacts
) -> ValidationReport:
    """Run the deterministic checks and move the instruction to the matching status."""
    answered = {str(item["question_id"]) for item in instruction.clarification_answers or []}
    report = validate_requirement(
        analysis, facts, instruction_text=instruction.raw_text, answered_question_ids=answered
    )
    instruction.analysis = analysis.model_dump(mode="json")
    instruction.validation = {
        "ready": report.ready,
        "issues": [asdict(issue) for issue in report.issues],
    }
    instruction.clarification_questions = [q.model_dump(mode="json") for q in report.questions]
    instruction.status = "analysed" if report.ready else "needs_clarification"
    instruction.error = None
    return report


# --- API side ------------------------------------------------------------------------------


def plan_scope(analysis: RequirementAnalysis, warnings: list[str]) -> dict[str, Any]:
    """What a test plan says, taken from an analysed requirement. Nothing is added."""
    return {
        "feature_name": analysis.feature_name,
        "application_context": analysis.application_context.model_dump(),
        "objectives": [
            {
                **objective.model_dump(),
                "expected_behavior": (
                    b.description if (b := analysis.behavior_for(objective.id)) else None
                ),
                "expected_source": (b.source if b else None),
            }
            for objective in analysis.test_objectives
        ],
        "business_rules": [rule.model_dump() for rule in analysis.business_rules],
        "required_test_data": [d.model_dump() for d in analysis.required_test_data],
        "preconditions": analysis.preconditions,
        "constraints": analysis.constraints,
        "assumptions": analysis.assumptions,
        "warnings": warnings,
    }


class InstructionService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        audit: AuditService,
        jobs: JobQueue,
        gateway: LLMGateway,
        redis: RedisClient,
        settings: Settings,
    ) -> None:
        self._session = session
        self._audit = audit
        self._jobs = jobs
        self._gateway = gateway
        self._limiter = RateLimiter(redis)
        self._settings = settings

    def _repository(self, ctx: ProjectContext) -> InstructionRepository:
        return InstructionRepository(self._session, ctx.organization_id)

    # --- reads -------------------------------------------------------------------

    async def list(
        self, ctx: ProjectContext, *, status: str | None, limit: int
    ) -> Sequence[TestInstruction]:
        return await self._repository(ctx).list_in_project(
            ctx.project.id, status=status, limit=limit
        )

    async def get(self, ctx: ProjectContext, instruction_id: uuid.UUID) -> TestInstruction:
        instruction = await self._repository(ctx).get_in_project(ctx.project.id, instruction_id)
        if instruction is None:
            raise NotFoundError("Instruction not found.")
        return instruction

    async def usage(self, ctx: ProjectContext, instruction: TestInstruction) -> UsageTotals:
        return await AgentLogRepository(self._session, ctx.organization_id).usage_for_instruction(
            instruction.id
        )

    async def plan(self, ctx: ProjectContext, instruction: TestInstruction) -> TestPlan:
        plan = await PlanRepository(self._session, ctx.organization_id).latest(instruction.id)
        if plan is None:
            raise NotFoundError("This instruction has no plan yet.")
        return plan

    # --- submit and change -------------------------------------------------------

    async def submit(
        self, ctx: ProjectContext, data: InstructionCreate, *, analyse: bool = True
    ) -> TestInstruction:
        """`analyse=False` only stores the instruction; the agent workflow analyses it itself."""
        ctx.require(Permission.INSTRUCTIONS_CREATE)
        ctx.require_writable()
        self._check_text(data.text)
        await self._limiter.enforce(
            "instructions",
            str(ctx.organization_id),
            limit=self._settings.instruction_limit,
            window_s=self._settings.instruction_window_s,
        )

        # The application and environment are whatever the caller names, checked to be
        # in this project. The model never chooses or changes the target.
        application = await ApplicationRepository(
            self._session, ctx.organization_id
        ).get_in_project(ctx.project.id, data.application_id)
        environment = (
            await EnvironmentRepository(self._session, ctx.organization_id).get_in_application(
                application.id, data.environment_id
            )
            if application
            else None
        )
        if application is None or environment is None:
            raise ValidationFailedError(
                "The application or environment does not exist in this project.",
                code="unknown_target",
                errors=[
                    ErrorDetail(field="environment_id", code="unknown_target", message="not found")
                ],
            )

        instruction = TestInstruction(
            organization_id=ctx.organization_id,
            project_id=ctx.project.id,
            application_id=application.id,
            environment_id=environment.id,
            created_by=ctx.user.id,
            raw_text=clean_text(data.text, max_chars=self._settings.instruction_max_chars),
            test_types=list(data.test_types),
            options=data.options.model_dump(mode="json"),
            status="received",
            clarification_questions=[],
            clarification_answers=[],
        )
        await self._repository(ctx).add(instruction)
        await self._record(
            ctx, "instruction.submit", instruction, {"environment": environment.name}
        )
        if analyse:
            await self._start_analysis(ctx, instruction)
        return instruction

    async def start_analysis(self, ctx: ProjectContext, instruction: TestInstruction) -> None:
        """Mark the instruction as being analysed and hand the job to the queue."""
        self._require_not_terminal(instruction)
        await self._start_analysis(ctx, instruction)

    async def update(
        self, ctx: ProjectContext, instruction: TestInstruction, data: InstructionUpdate
    ) -> TestInstruction:
        self._require_owner_or_manager(ctx, instruction)
        self._require_not_terminal(instruction)
        self._require_not_running(instruction)
        if data.text is not None:
            self._check_text(data.text)
            instruction.raw_text = clean_text(
                data.text, max_chars=self._settings.instruction_max_chars
            )
        if data.test_types is not None:
            instruction.test_types = list(data.test_types)
        if data.options is not None:
            instruction.options = data.options.model_dump(mode="json")
        # The old analysis and answers described a different requirement.
        instruction.analysis = None
        instruction.validation = None
        instruction.analysis_source = None
        instruction.clarification_questions = []
        instruction.clarification_answers = []
        await self._record(ctx, "instruction.update", instruction, {})
        await self._start_analysis(ctx, instruction)
        return instruction

    async def reanalyse(self, ctx: ProjectContext, instruction: TestInstruction) -> TestInstruction:
        self._require_owner_or_manager(ctx, instruction)
        self._require_not_terminal(instruction)
        self._require_not_running(instruction)
        await self._start_analysis(ctx, instruction)
        return instruction

    async def clarify(
        self,
        ctx: ProjectContext,
        instruction: TestInstruction,
        answers: Sequence[ClarificationAnswer],
    ) -> TestInstruction:
        ctx.require(Permission.INSTRUCTIONS_CREATE)
        ctx.require_writable()
        if instruction.status != "needs_clarification":
            raise ConflictError(
                "This instruction is not waiting for clarification.",
                code="invalid_state_transition",
            )
        open_questions = {
            str(q["id"]): str(q["question"]) for q in instruction.clarification_questions or []
        }
        unknown = [a.question_id for a in answers if a.question_id not in open_questions]
        if unknown or len({a.question_id for a in answers}) != len(answers):
            raise ValidationFailedError(
                "Answers must refer to open questions, once each.",
                code="unknown_question",
                errors=[
                    ErrorDetail(field="answers", code="unknown_question", message=question_id)
                    for question_id in unknown
                ],
            )
        now = datetime.now(UTC).isoformat()
        instruction.clarification_answers = [
            *(instruction.clarification_answers or []),
            *(
                {
                    "question_id": answer.question_id,
                    "question": open_questions[answer.question_id],
                    "answer": clean_text(answer.answer, max_chars=1000),
                    "answered_by": str(ctx.user.id),
                    "answered_at": now,
                }
                for answer in answers
            ),
        ]
        await self._record(
            ctx, "instruction.clarify", instruction, {"questions": [a.question_id for a in answers]}
        )
        await self._start_analysis(ctx, instruction)
        return instruction

    async def edit_analysis(
        self, ctx: ProjectContext, instruction: TestInstruction, analysis: RequirementAnalysis
    ) -> TestInstruction:
        """A person corrects the structured requirement. The same checks apply to it."""
        self._require_owner_or_manager(ctx, instruction)
        if instruction.status not in ("analysed", "needs_clarification"):
            raise ConflictError(
                "Only an analysed instruction can be edited.", code="invalid_state_transition"
            )
        _, _, facts, _ = await load_target(self._session, instruction)
        apply_validation(instruction, analysis, facts)
        instruction.analysis_source = "human_edited"
        instruction.analysed_at = datetime.now(UTC)
        await self._session.flush()
        await self._record(ctx, "instruction.analysis_edit", instruction, {})
        return instruction

    async def revalidate(
        self, ctx: ProjectContext, instruction: TestInstruction
    ) -> TestInstruction:
        """Repeat the deterministic checks, e.g. after a missing credential was added. No LLM call."""
        ctx.require(Permission.INSTRUCTIONS_CREATE)
        if instruction.analysis is None or instruction.status not in (
            "analysed",
            "needs_clarification",
        ):
            raise ConflictError("There is no analysis to check.", code="invalid_state_transition")
        _, _, facts, _ = await load_target(self._session, instruction)
        apply_validation(
            instruction, RequirementAnalysis.model_validate(instruction.analysis), facts
        )
        await self._session.flush()
        return instruction

    async def cancel(self, ctx: ProjectContext, instruction: TestInstruction) -> TestInstruction:
        self._require_owner_or_manager(ctx, instruction)
        self._require_not_terminal(instruction)
        instruction.status = "cancelled"
        instruction.run_token = None  # a running analysis will discard its result
        await self._session.flush()
        await self._record(ctx, "instruction.cancel", instruction, {})
        return instruction

    async def approve(self, ctx: ProjectContext, instruction: TestInstruction) -> TestPlan:
        """Approve the analysed requirement as the plan to generate tests from.

        Only a person with the right permission can do this, and only when the
        deterministic checks pass. Nothing the model returns can approve anything.
        """
        ctx.require(Permission.INSTRUCTIONS_APPROVE)
        ctx.require_writable()
        if instruction.status != "analysed" or instruction.analysis is None:
            raise ConflictError(
                "Only a fully analysed instruction can be approved.",
                code="invalid_state_transition",
            )
        _, environment, facts, _ = await load_target(self._session, instruction)
        if (
            environment is not None
            and environment.kind == "production"
            and not has_permission(ctx.org.role, Permission.ENVIRONMENTS_SECURITY)
        ):
            raise ForbiddenError(
                "Only organization admins can approve tests against production.",
                code="admin_required",
            )
        # Checked again at the moment of approval: things may have changed since the analysis.
        analysis = RequirementAnalysis.model_validate(instruction.analysis)
        report = apply_validation(instruction, analysis, facts)
        if not report.ready:
            await self._session.flush()
            raise ConflictError(
                "The requirement has open questions or blocking issues.",
                code="requirement_not_ready",
            )

        plans = PlanRepository(self._session, ctx.organization_id)
        previous = await plans.latest(instruction.id)
        await plans.supersede_all(instruction.id)
        now = datetime.now(UTC)
        plan = await plans.add(
            TestPlan(
                organization_id=ctx.organization_id,
                instruction_id=instruction.id,
                version=(previous.version + 1) if previous else 1,
                status="approved",
                summary=analysis.summary,
                scope=plan_scope(
                    analysis, [i.message for i in report.issues if i.severity == "warning"]
                ),
                model=instruction.analysis_model,
                approved_by=ctx.user.id,
                approved_at=now,
            )
        )
        instruction.status = "approved"
        await self._session.flush()
        await self._record(
            ctx,
            "instruction.approve",
            instruction,
            {
                "plan_id": str(plan.id),
                "version": plan.version,
                "analysis_source": instruction.analysis_source,
            },
        )
        return plan

    # --- helpers -----------------------------------------------------------------

    async def _start_analysis(self, ctx: ProjectContext, instruction: TestInstruction) -> None:
        ctx.require(Permission.INSTRUCTIONS_CREATE)
        ctx.require_writable()
        if not self._gateway.configured:
            raise ServiceUnavailableError(
                "AI analysis is not configured on this installation.", code="llm_not_configured"
            )
        if instruction.llm_call_count >= self._settings.instruction_max_llm_calls:
            raise RateLimitedError(
                "This instruction has used all of its AI analysis attempts. "
                "Edit the analysis by hand, or submit a new instruction.",
                code="analysis_limit_reached",
            )
        await require_budget(
            self._session, ctx.organization_id, self._settings, ctx.org.organization.settings
        )
        token = secrets.token_hex(16)
        instruction.status = "analysing"
        instruction.run_token = token
        instruction.error = None
        await self._session.flush()
        await self._jobs.enqueue(
            ANALYSE_JOB,
            organization_id=str(ctx.organization_id),
            instruction_id=str(instruction.id),
            run_token=token,
        )

    def _check_text(self, text: str) -> None:
        if len(text) > self._settings.instruction_max_chars:
            raise ValidationFailedError(
                f"The instruction is longer than {self._settings.instruction_max_chars} characters.",
                code="instruction_too_long",
                errors=[ErrorDetail(field="text", code="too_long", message="too long")],
            )

    @staticmethod
    def _require_owner_or_manager(ctx: ProjectContext, instruction: TestInstruction) -> None:
        ctx.require(Permission.INSTRUCTIONS_CREATE)
        ctx.require_writable()
        if instruction.created_by != ctx.user.id and not ctx.can_manage:
            raise ForbiddenError(
                "Only the author or a project manager can change this instruction.",
                code="insufficient_role",
            )

    @staticmethod
    def _require_not_terminal(instruction: TestInstruction) -> None:
        if instruction.status in _TERMINAL:
            raise ConflictError(
                f"This instruction is {instruction.status} and can no longer be changed.",
                code="invalid_state_transition",
            )

    def _require_not_running(self, instruction: TestInstruction) -> None:
        if instruction.status != "analysing":
            return
        stale = datetime.now(UTC) - instruction.updated_at > timedelta(
            seconds=self._settings.analysis_stale_after_s
        )
        if not stale:
            raise ConflictError("An analysis is already running.", code="analysis_in_progress")

    async def _record(
        self,
        ctx: ProjectContext,
        action: str,
        instruction: TestInstruction,
        changes: dict[str, Any],
    ) -> None:
        await self._audit.record(
            self._session,
            AuditEvent(
                action=action,
                actor_id=ctx.user.id,
                organization_id=ctx.organization_id,
                resource_type="test_instruction",
                resource_id=instruction.id,
                changes=changes,
            ),
        )


# --- worker side ---------------------------------------------------------------------------


class AnalysisRunner:
    """Runs one analysis: read, call the model without holding a transaction, store."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        gateway: LLMGateway,
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._gateway = gateway
        self._settings = settings

    async def run(
        self, *, organization_id: uuid.UUID, instruction_id: uuid.UUID, run_token: str
    ) -> str:
        """Returns the resulting status, or "discarded" if the run was no longer wanted."""
        claimed = await self._claim(organization_id, instruction_id, run_token)
        if claimed is None:
            return "discarded"
        text, context, clarifications = claimed

        recorder = LLMUsageRecorder(
            self._session_factory,
            organization_id=organization_id,
            instruction_id=instruction_id,
            node="analyse_requirement",
        )
        analysis: RequirementAnalysis | None = None
        error: dict[str, str] | None = None
        model: str | None = None
        try:
            result = await analyse_requirement(
                self._gateway,
                instruction=text,
                context=context,
                clarifications=clarifications,
                max_chars=self._settings.instruction_max_chars,
                recorder=recorder,
            )
            analysis, model = result.output, result.model
        except LLMError as exc:
            error = {"code": exc.code, "message": exc.message}
        except Exception:
            logger.exception("analysis_failed", extra={"instruction_id": str(instruction_id)})
            error = {"code": "internal_error", "message": "The analysis failed unexpectedly."}

        async with self._session_factory() as session, session.begin():
            await set_current_org(session, organization_id)
            instruction = await InstructionRepository(session, organization_id).get_for_update(
                instruction_id
            )
            # Edited, cancelled or restarted while the model was working: drop this result.
            if instruction is None or instruction.run_token != run_token:
                return "discarded"
            instruction.run_token = None
            if analysis is None:
                instruction.status = "failed"
                instruction.error = error
                return "failed"
            _, _, facts, _ = await load_target(session, instruction)
            apply_validation(instruction, analysis, facts)
            instruction.analysis_source = "ai"
            instruction.analysis_model = model
            instruction.analysis_prompt = get_prompt(PROMPT_NAME).ref
            instruction.analysed_at = datetime.now(UTC)
            return instruction.status

    async def _claim(
        self, organization_id: uuid.UUID, instruction_id: uuid.UUID, run_token: str
    ) -> tuple[str, AnalysisContext, list[Clarification]] | None:
        # The job can be picked up before the request that enqueued it has committed,
        # so wait briefly for the token to appear before concluding the run is stale.
        for attempt in range(8):
            async with self._session_factory() as session, session.begin():
                await set_current_org(session, organization_id)
                instruction = await InstructionRepository(session, organization_id).get_for_update(
                    instruction_id
                )
                if (
                    instruction is not None
                    and instruction.status == "analysing"
                    and instruction.run_token == run_token
                ):
                    instruction.llm_call_count += 1
                    _, _, _, context = await load_target(session, instruction)
                    clarifications = [
                        Clarification(question=str(a["question"]), answer=str(a["answer"]))
                        for a in instruction.clarification_answers or []
                    ]
                    return instruction.raw_text, context, clarifications
            if attempt < 7:
                await asyncio.sleep(0.25)
        return None
