"""Test cases: generation, review, versions and approval.

Versions are the heart of the review model. Every change creates a new,
immutable version:

- version 1 of a generated case is exactly what the model wrote (`source: ai`)
  and stays reachable as the case's `original_version`;
- an edit by a person is a new version (`source: human`);
- approving marks one specific version as approved. A later edit creates a newer
  version and the case needs review again, while the approved version remains.

`TestCaseService` is what the API calls. `GenerationRunner` is what the worker runs.
"""

import asyncio
import logging
import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.llm.base import LLMError, LLMGateway
from app.agent.nodes.generate_test_cases import (
    PROMPT_NAME,
    GenerationContext,
    RewriteRequest,
    generate_test_cases,
)
from app.agent.prompts import get_prompt
from app.core.config import Settings
from app.core.errors import (
    ConflictError,
    ErrorDetail,
    NotFoundError,
    RateLimitedError,
    ServiceUnavailableError,
    ValidationFailedError,
)
from app.core.jobs import JobQueue
from app.core.security.permissions import Permission
from app.core.security.url_safety import Origin
from app.db.context import set_current_org
from app.models import (
    TestCase,
    TestCaseVersion,
    TestInstruction,
    TestPlan,
    TestStep,
    TestSuite,
)
from app.repositories.instructions import InstructionRepository, PlanRepository
from app.repositories.projects import (
    ApplicationRepository,
    CredentialRepository,
    EnvironmentRepository,
)
from app.repositories.test_cases import CaseVersionRepository, SuiteRepository, TestCaseRepository
from app.schemas.instructions import InstructionOptions
from app.services.audit import AuditEvent, AuditService
from app.services.automation import automation_issues
from app.services.llm_usage import LLMUsageRecorder, require_budget
from app.services.projects import ProjectContext
from app.testing.automation import Script
from app.testing.case_validation import (
    CaseFacts,
    CaseReport,
    index_cases,
    remove_duplicates,
    validate_case,
)
from app.testing.cases import CaseContent, CaseStep, GeneratedCase, TestDataItem, Verification

logger = logging.getLogger(__name__)

GENERATE_JOB = "generate_cases"
# Stored per assertion step; the table predates the case-level source and uses its own words.
_STEP_SOURCE = {
    "plan_objective": "user_specified",
    "configured_rule": "derived_rule",
    "human_specified": "user_specified",
    "unknown": "llm_inferred",
}


@dataclass(frozen=True, slots=True)
class CaseView:
    case: TestCase
    version: TestCaseVersion
    content: CaseContent


# --- shared by the service and the runner -------------------------------------------------


def content_of(version: TestCaseVersion, steps: Sequence[TestStep]) -> CaseContent:
    return CaseContent(
        title=version.title,
        description=version.description,
        category=version.category,
        test_type=version.test_type,
        objective_id=version.objective_id,
        preconditions=[str(p) for p in version.preconditions],
        test_data=[TestDataItem.model_validate(item) for item in version.test_data],
        steps=[
            CaseStep(
                action=step.action,
                description=step.description or "",
                target=(step.target or {}).get("description"),
                value=step.args.get("value"),
                side_effect=step.side_effect,
            )
            for step in steps
        ],
        expected_result=version.expected_result,
        expected_source=version.expected_source,
        priority=version.priority,
        severity=version.severity,
        tags=list(version.tags),
        dependencies=[str(d) for d in version.dependencies],
        automation_feasibility=version.automation_feasibility,
        automation_notes=version.automation_notes,
        verification=Verification.model_validate(version.verification),
        assumptions=[str(a) for a in version.assumptions],
    )


async def load_views(
    session: AsyncSession, organization_id: uuid.UUID, cases: Sequence[TestCase]
) -> list[CaseView]:
    versions_repo = CaseVersionRepository(session, organization_id)
    version_ids = [c.current_version_id for c in cases if c.current_version_id]
    versions = await versions_repo.by_ids(version_ids)
    steps = await versions_repo.steps(version_ids)
    views = []
    for case in cases:
        version = versions.get(case.current_version_id) if case.current_version_id else None
        if version is not None:
            views.append(CaseView(case, version, content_of(version, steps[version.id])))
    return views


async def build_facts(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    environment_id: uuid.UUID | None,
    instruction_id: uuid.UUID | None,
    exclude_case_id: uuid.UUID | None = None,
) -> CaseFacts:
    """Collect what the validator compares a case against."""
    environments = EnvironmentRepository(session, organization_id)
    environment = await environments.get(environment_id) if environment_id else None
    origins = (await environments.origins([environment.id]))[environment.id] if environment else []
    credentials = (
        await CredentialRepository(session, organization_id).list_in_environment(environment.id)
        if environment
        else []
    )
    objective_ids: set[str] = set()
    if instruction_id is not None:
        plan = await PlanRepository(session, organization_id).latest(instruction_id)
        if plan is not None:
            objective_ids = {str(o["id"]) for o in plan.scope.get("objectives", [])}

    # Duplicates are looked for among the other cases of the same instruction (or,
    # for manual cases, among the project's other manual cases).
    others = await TestCaseRepository(session, organization_id).list_in_project(
        project_id, instruction_id=instruction_id
    )
    siblings = [
        c
        for c in others
        if c.id != exclude_case_id and (instruction_id is not None or c.instruction_id is None)
    ]
    titles, signatures = index_cases(
        (view.case.key, view.content)
        for view in await load_views(session, organization_id, siblings)
    )
    return CaseFacts(
        objective_ids=frozenset(objective_ids),
        credential_roles=frozenset(c.role_label for c in credentials),
        allowed_origins=frozenset(Origin(o.scheme, o.host, o.port) for o in origins),
        environment_name=environment.name if environment else "(no environment)",
        environment_is_read_only=environment.is_read_only if environment else False,
        environment_allowed_actions=frozenset(environment.allowed_actions)
        if environment
        # A case without an environment is not restricted until it is given one.
        else frozenset(
            {
                "navigate",
                "read",
                "click",
                "type",
                "select",
                "upload",
                "download",
                "api_read",
                "api_write",
            }
        ),
        other_titles=titles,
        other_signatures=signatures,
    )


async def write_version(
    session: AsyncSession,
    case: TestCase,
    content: CaseContent,
    report: CaseReport,
    *,
    source: str,
    change_reason: str,
    created_by: uuid.UUID | None,
    automation: dict[str, Any] | None = None,
) -> TestCaseVersion:
    """Add a new immutable version and make it the case's current one."""
    versions = CaseVersionRepository(session, case.organization_id)
    number = len(await versions.list_for_case(case.id)) + 1
    version = TestCaseVersion(
        organization_id=case.organization_id,
        test_case_id=case.id,
        version=number,
        source=source,
        change_reason=change_reason,
        created_by=created_by,
        title=content.title,
        description=content.description,
        category=content.category,
        test_type=content.test_type,
        objective_id=content.objective_id,
        preconditions=list(content.preconditions),
        test_data=[item.model_dump() for item in content.test_data],
        expected_result=content.expected_result,
        expected_source=content.expected_source,
        priority=content.priority,
        severity=content.severity,
        tags=list(content.tags),
        dependencies=list(content.dependencies),
        automation_feasibility=content.automation_feasibility,
        automation_notes=content.automation_notes,
        verification=content.verification.model_dump(),
        assumptions=list(content.assumptions),
        validation=report.as_dict(),
        oracle_status="none" if content.expected_source == "unknown" else "verified",
        automation=automation,
    )
    await versions.add(version)
    session.add_all(
        TestStep(
            organization_id=case.organization_id,
            test_case_version_id=version.id,
            position=position,
            action=step.action,
            target={"description": step.target} if step.target else None,
            args={"value": step.value} if step.value is not None else {},
            description=step.description,
            is_assertion=step.is_assertion,
            expected_source=_STEP_SOURCE[content.expected_source] if step.is_assertion else None,
            side_effect=step.side_effect,
        )
        for position, step in enumerate(content.steps, start=1)
    )
    # Mirror what lists and filters need, and reset the review state.
    case.title = content.title
    case.description = content.description
    case.type = content.test_type
    case.category = content.category
    case.priority = content.priority
    case.severity = content.severity
    case.tags = list(content.tags)
    case.current_version_id = version.id
    case.status = "needs_review"
    await session.flush()
    return version


async def new_case(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    application_id: uuid.UUID,
    environment_id: uuid.UUID | None,
    instruction_id: uuid.UUID | None,
    plan_id: uuid.UUID | None,
    source: str,
    created_by: uuid.UUID | None,
    content: CaseContent,
) -> TestCase:
    cases = TestCaseRepository(session, organization_id)
    number = await cases.next_key_number(project_id)
    case = TestCase(
        organization_id=organization_id,
        project_id=project_id,
        application_id=application_id,
        environment_id=environment_id,
        instruction_id=instruction_id,
        plan_id=plan_id,
        key=f"TC-{number:04d}",
        title=content.title,
        type=content.test_type,
        category=content.category,
        priority=content.priority,
        severity=content.severity,
        source=source,
        status="needs_review",
        created_by=created_by,
    )
    return await cases.add(case)


def begin_generation(instruction: TestInstruction, *, case_ids: Sequence[str] = ()) -> str:
    """Mark an instruction as generating and return the token of this run.

    The caller starts `GenerationRunner` with the token; a run whose token no
    longer matches discards its result.
    """
    token = secrets.token_hex(16)
    instruction.generation = {
        "state": "running",
        "mode": "regenerate" if case_ids else "full",
        "previous_status": instruction.status,
    }
    instruction.status = "generating"
    instruction.run_token = token
    return token


# --- API side ------------------------------------------------------------------------------


class TestCaseService:
    __test__ = False

    def __init__(
        self,
        *,
        session: AsyncSession,
        audit: AuditService,
        jobs: JobQueue,
        gateway: LLMGateway,
        settings: Settings,
    ) -> None:
        self._session = session
        self._audit = audit
        self._jobs = jobs
        self._gateway = gateway
        self._settings = settings

    def _cases(self, ctx: ProjectContext) -> TestCaseRepository:
        return TestCaseRepository(self._session, ctx.organization_id)

    # --- reads -------------------------------------------------------------------

    async def list_views(
        self,
        ctx: ProjectContext,
        *,
        instruction_id: uuid.UUID | None = None,
        status: str | None = None,
        category: str | None = None,
    ) -> list[CaseView]:
        cases = await self._cases(ctx).list_in_project(
            ctx.project.id, instruction_id=instruction_id, status=status, category=category
        )
        return await load_views(self._session, ctx.organization_id, cases)

    async def get(self, ctx: ProjectContext, case_id: uuid.UUID) -> TestCase:
        case = await self._cases(ctx).get_in_project(ctx.project.id, case_id)
        if case is None:
            raise NotFoundError("Test case not found.")
        return case

    async def view(self, ctx: ProjectContext, case: TestCase) -> CaseView:
        views = await load_views(self._session, ctx.organization_id, [case])
        if not views:
            raise NotFoundError("Test case not found.")
        return views[0]

    async def versions(self, ctx: ProjectContext, case: TestCase) -> Sequence[TestCaseVersion]:
        return await CaseVersionRepository(self._session, ctx.organization_id).list_for_case(
            case.id
        )

    async def version(
        self, ctx: ProjectContext, case: TestCase, number: int
    ) -> tuple[TestCaseVersion, CaseContent]:
        repository = CaseVersionRepository(self._session, ctx.organization_id)
        version = await repository.get_number(case.id, number)
        if version is None:
            raise NotFoundError("Version not found.")
        steps = (await repository.steps([version.id]))[version.id]
        return version, content_of(version, steps)

    async def version_numbers(self, ctx: ProjectContext, case: TestCase) -> dict[uuid.UUID, int]:
        return {v.id: v.version for v in await self.versions(ctx, case)}

    # --- writing -----------------------------------------------------------------

    async def create_manual(
        self,
        ctx: ProjectContext,
        *,
        application_id: uuid.UUID,
        environment_id: uuid.UUID | None,
        instruction_id: uuid.UUID | None,
        content: CaseContent,
    ) -> TestCase:
        ctx.require(Permission.CASES_MANAGE)
        ctx.require_writable()
        application = await ApplicationRepository(
            self._session, ctx.organization_id
        ).get_in_project(ctx.project.id, application_id)
        environment = (
            await EnvironmentRepository(self._session, ctx.organization_id).get_in_application(
                application_id, environment_id
            )
            if application and environment_id
            else None
        )
        instruction = (
            await InstructionRepository(self._session, ctx.organization_id).get_in_project(
                ctx.project.id, instruction_id
            )
            if instruction_id
            else None
        )
        if (
            application is None
            or (environment_id and environment is None)
            or (instruction_id and instruction is None)
        ):
            raise ValidationFailedError(
                "The application, environment or instruction is not in this project.",
                code="unknown_target",
            )
        content = self._as_human_content(content, previous=None)
        case = await new_case(
            self._session,
            organization_id=ctx.organization_id,
            project_id=ctx.project.id,
            application_id=application.id,
            environment_id=environment.id if environment else None,
            instruction_id=instruction.id if instruction else None,
            plan_id=None,
            source="manual",
            created_by=ctx.user.id,
            content=content,
        )
        report = validate_case(content, await self._facts(ctx, case))
        await write_version(
            self._session,
            case,
            content,
            report,
            source="human",
            change_reason="created",
            created_by=ctx.user.id,
        )
        await self._record(ctx, "test_case.create", case, {"key": case.key})
        return case

    async def edit(self, ctx: ProjectContext, case: TestCase, content: CaseContent) -> TestCase:
        """Save a person's changes as a new version. Earlier versions are untouched."""
        ctx.require(Permission.CASES_MANAGE)
        ctx.require_writable()
        current = await self.view(ctx, case)
        content = self._as_human_content(content, previous=current.content)
        if content == current.content:
            return case
        # The script stays with the case; it is checked again against the new content.
        automation = current.version.automation
        report = await self._report(ctx, case, content, automation)
        version = await write_version(
            self._session,
            case,
            content,
            report,
            source="human",
            change_reason="edited",
            created_by=ctx.user.id,
            automation=automation,
        )
        await self._record(
            ctx, "test_case.edit", case, {"key": case.key, "version": version.version}
        )
        return case

    async def set_automation(
        self,
        ctx: ProjectContext,
        case: TestCase,
        script: Script | None,
        *,
        change_reason: str = "edited",
    ) -> TestCase:
        """Attach, replace or remove the executable script. Always a new version.

        The new version needs review and approval like any other, whatever the reason.
        """
        ctx.require(Permission.CASES_MANAGE)
        ctx.require_writable()
        current = await self.view(ctx, case)
        automation = script.model_dump(mode="json", exclude_none=True) if script else None
        if automation == current.version.automation:
            return case
        report = await self._report(ctx, case, current.content, automation)
        version = await write_version(
            self._session,
            case,
            current.content,
            report,
            source="human",
            change_reason=change_reason,
            created_by=ctx.user.id,
            automation=automation,
        )
        await self._record(
            ctx,
            "test_case.automation",
            case,
            {
                "key": case.key,
                "version": version.version,
                "steps": len(script.steps) if script else 0,
                "removed": script is None,
            },
        )
        return case

    async def _report(
        self,
        ctx: ProjectContext,
        case: TestCase,
        content: CaseContent,
        automation: dict[str, Any] | None,
    ) -> CaseReport:
        report = validate_case(content, await self._facts(ctx, case))
        report.issues.extend(
            await automation_issues(
                self._session,
                organization_id=ctx.organization_id,
                project_id=ctx.project.id,
                environment_id=case.environment_id,
                content=content,
                automation=automation,
            )
        )
        return report

    async def delete(self, ctx: ProjectContext, case: TestCase) -> None:
        ctx.require(Permission.CASES_MANAGE)
        ctx.require_writable()
        suites = SuiteRepository(self._session, ctx.organization_id)
        affected = await suites.suites_containing(case.id)
        await suites.remove_case_everywhere(case.id)
        await suites.reopen(affected)
        case.deleted_at = datetime.now(UTC)
        await self._session.flush()
        await self._record(ctx, "test_case.delete", case, {"key": case.key})

    async def approve(self, ctx: ProjectContext, case: TestCase) -> TestCase:
        ctx.require(Permission.CASES_APPROVE)
        ctx.require_writable()
        await approve_case(self._session, ctx, case)
        await self._record(ctx, "test_case.approve", case, {"key": case.key})
        return case

    # --- generation --------------------------------------------------------------

    async def generate(self, ctx: ProjectContext, instruction: TestInstruction) -> TestInstruction:
        """Start writing cases for an instruction whose plan has been approved."""
        if instruction.status != "approved":
            raise ConflictError(
                "Test cases can be generated once the requirement has been approved.",
                code="invalid_state_transition",
            )
        await self._start(ctx, instruction, case_ids=[], feedback=None)
        return instruction

    async def regenerate(
        self,
        ctx: ProjectContext,
        instruction: TestInstruction,
        *,
        case_ids: Sequence[uuid.UUID],
        feedback: str | None,
    ) -> TestInstruction:
        """Ask the model to rewrite selected cases. Each gets a new version; nothing is lost."""
        if instruction.status != "awaiting_approval":
            raise ConflictError(
                "Cases can be regenerated while they are under review.",
                code="invalid_state_transition",
            )
        cases = await self._cases(ctx).list_in_project(
            ctx.project.id, instruction_id=instruction.id, ids=list(case_ids)
        )
        if len(cases) != len(set(case_ids)):
            raise ValidationFailedError(
                "Some of the selected cases do not belong to this instruction.",
                code="unknown_case",
                errors=[ErrorDetail(field="case_ids", code="unknown_case", message="not found")],
            )
        await self._start(ctx, instruction, case_ids=[str(c.id) for c in cases], feedback=feedback)
        return instruction

    async def _start(
        self,
        ctx: ProjectContext,
        instruction: TestInstruction,
        *,
        case_ids: list[str],
        feedback: str | None,
    ) -> None:
        ctx.require(Permission.CASES_MANAGE)
        ctx.require_writable()
        if not self._gateway.configured:
            raise ServiceUnavailableError(
                "AI generation is not configured on this installation.", code="llm_not_configured"
            )
        if instruction.llm_call_count >= self._settings.instruction_max_llm_calls * 5:
            raise RateLimitedError(
                "This instruction has used all of its AI attempts.", code="generation_limit_reached"
            )
        await require_budget(
            self._session, ctx.organization_id, self._settings, ctx.org.organization.settings
        )
        token = secrets.token_hex(16)
        instruction.generation = {
            "state": "running",
            "mode": "regenerate" if case_ids else "full",
            "previous_status": instruction.status,
        }
        instruction.status = "generating"
        instruction.run_token = token
        await self._session.flush()
        await self._record_instruction(
            ctx, instruction, "regenerate" if case_ids else "generate", {"cases": len(case_ids)}
        )
        await self._jobs.enqueue(
            GENERATE_JOB,
            organization_id=str(ctx.organization_id),
            instruction_id=str(instruction.id),
            run_token=token,
            case_ids=case_ids,
            feedback=feedback,
        )

    # --- helpers -----------------------------------------------------------------

    async def _facts(self, ctx: ProjectContext, case: TestCase) -> CaseFacts:
        return await build_facts(
            self._session,
            organization_id=ctx.organization_id,
            project_id=ctx.project.id,
            environment_id=case.environment_id,
            instruction_id=case.instruction_id,
            exclude_case_id=case.id,
        )

    @staticmethod
    def _as_human_content(content: CaseContent, *, previous: CaseContent | None) -> CaseContent:
        """Label the expected result truthfully when a person saves a case.

        A person cannot claim a result "comes from the plan" by typing it: once
        they write or change it, its source is them.
        """
        if content.expected_result is None:
            return content.model_copy(update={"expected_source": "unknown"})
        unchanged = (
            previous is not None
            and previous.expected_result == content.expected_result
            and previous.expected_source != "unknown"
        )
        source = previous.expected_source if unchanged and previous else "human_specified"
        return content.model_copy(update={"expected_source": source})

    async def _record(
        self, ctx: ProjectContext, action: str, case: TestCase, changes: dict[str, Any]
    ) -> None:
        await self._audit.record(
            self._session,
            AuditEvent(
                action=action,
                actor_id=ctx.user.id,
                organization_id=ctx.organization_id,
                resource_type="test_case",
                resource_id=case.id,
                changes=changes,
            ),
        )

    async def _record_instruction(
        self, ctx: ProjectContext, instruction: TestInstruction, what: str, changes: dict[str, Any]
    ) -> None:
        await self._audit.record(
            self._session,
            AuditEvent(
                action=f"instruction.{what}",
                actor_id=ctx.user.id,
                organization_id=ctx.organization_id,
                resource_type="test_instruction",
                resource_id=instruction.id,
                changes=changes,
            ),
        )


async def approve_case(session: AsyncSession, ctx: ProjectContext, case: TestCase) -> None:
    """Approve the case's current version, after checking it once more."""
    views = await load_views(session, ctx.organization_id, [case])
    if not views:
        raise NotFoundError("Test case not found.")
    view = views[0]
    facts = await build_facts(
        session,
        organization_id=ctx.organization_id,
        project_id=ctx.project.id,
        environment_id=case.environment_id,
        instruction_id=case.instruction_id,
        exclude_case_id=case.id,
    )
    report = validate_case(view.content, facts)
    report.issues.extend(
        await automation_issues(
            session,
            organization_id=ctx.organization_id,
            project_id=ctx.project.id,
            environment_id=case.environment_id,
            content=view.content,
            automation=view.version.automation,
        )
    )
    view.version.validation = report.as_dict()
    if not report.ready:
        blocking = [i for i in report.issues if i.severity == "blocking"]
        raise ConflictError(
            f"{case.key} cannot be approved yet: {blocking[0].message}",
            code="case_not_ready",
            errors=[
                ErrorDetail(field=case.key, code=issue.code, message=issue.message)
                for issue in blocking
            ],
        )
    now = datetime.now(UTC)
    view.version.approved_by = ctx.user.id
    view.version.approved_at = now
    case.status = "approved"
    case.approved_version_id = view.version.id
    await session.flush()


# --- worker side ---------------------------------------------------------------------------


class GenerationRunner:
    """Writes or rewrites the test cases of one instruction."""

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
        self,
        *,
        organization_id: uuid.UUID,
        instruction_id: uuid.UUID,
        run_token: str,
        case_ids: Sequence[uuid.UUID],
        feedback: str | None,
    ) -> str:
        """Returns the instruction's resulting status, or "discarded"."""
        claimed = await self._claim(organization_id, instruction_id, run_token, case_ids)
        if claimed is None:
            return "discarded"
        plan_scope, context, existing_titles, rewrite_cases, previous_status = claimed

        recorder = LLMUsageRecorder(
            self._session_factory,
            organization_id=organization_id,
            instruction_id=instruction_id,
            node="generate_test_cases",
        )
        generated: list[GeneratedCase] = []
        error: dict[str, str] | None = None
        calls = 0
        objective_ids = [str(o["id"]) for o in plan_scope.get("objectives", [])]
        size = self._settings.generation_objectives_per_call
        batches: list[list[str]] = (
            [objective_ids]
            if rewrite_cases
            else [objective_ids[i : i + size] for i in range(0, len(objective_ids), size)] or [[]]
        )
        try:
            for batch in batches:
                room = self._settings.generation_max_cases - len(generated)
                if room <= 0:
                    break
                calls += 1
                result = await generate_test_cases(
                    self._gateway,
                    plan_scope=plan_scope,
                    objectives_to_cover=batch,
                    context=context,
                    existing_titles=existing_titles + [c.title for c in generated],
                    max_cases=len(rewrite_cases)
                    if rewrite_cases
                    else min(room, self._settings.generation_max_cases_per_call),
                    rewrite=RewriteRequest([c for _, c in rewrite_cases], feedback)
                    if rewrite_cases
                    else None,
                    recorder=recorder,
                )
                generated.extend(result.output.cases[:room])
        except LLMError as exc:
            error = {"code": exc.code, "message": exc.message}
        except Exception:
            logger.exception("generation_failed", extra={"instruction_id": str(instruction_id)})
            error = {"code": "internal_error", "message": "The generation failed unexpectedly."}

        async with self._session_factory() as session, session.begin():
            await set_current_org(session, organization_id)
            instruction = await InstructionRepository(session, organization_id).get_for_update(
                instruction_id
            )
            if instruction is None or instruction.run_token != run_token:
                return "discarded"
            instruction.run_token = None
            instruction.llm_call_count += calls
            summary: dict[str, Any] = {
                "state": "failed" if error and not generated else "finished",
                "mode": "regenerate" if rewrite_cases else "full",
                "llm_calls": calls,
                "prompt": get_prompt(PROMPT_NAME).ref,
            }
            if error:
                # What was produced before the failure is kept; the rest can be requested again.
                summary["error"] = error
                summary["partial"] = bool(generated)
            if rewrite_cases:
                summary |= await self._store_rewrites(
                    session, instruction, rewrite_cases, generated
                )
                instruction.status = "awaiting_approval"
            elif generated:
                summary |= await self._store_new(session, instruction, generated)
                instruction.status = "awaiting_approval"
            else:
                instruction.status = previous_status
            instruction.generation = summary
            return instruction.status

    # --- persistence -------------------------------------------------------------

    async def _store_new(
        self, session: AsyncSession, instruction: TestInstruction, generated: list[GeneratedCase]
    ) -> dict[str, Any]:
        org = instruction.organization_id
        unique, duplicates = remove_duplicates(generated)
        plan = await PlanRepository(session, org).latest(instruction.id)
        created: list[TestCase] = []
        needing_attention = 0
        skipped_existing = 0
        for item in unique:
            content = CaseContent.model_validate(item.model_dump(exclude={"local_id"}))
            facts = await build_facts(
                session,
                organization_id=org,
                project_id=instruction.project_id,
                environment_id=instruction.environment_id,
                instruction_id=instruction.id,
            )
            # Same scenario as a case this instruction already has: not created again.
            if content.title_key() in facts.other_titles or (
                content.steps and content.signature() in facts.other_signatures
            ):
                skipped_existing += 1
                continue
            report = validate_case(content, facts)
            case = await new_case(
                session,
                organization_id=org,
                project_id=instruction.project_id,
                application_id=instruction.application_id,
                environment_id=instruction.environment_id,
                instruction_id=instruction.id,
                plan_id=plan.id if plan else None,
                source="ai_generated",
                created_by=None,
                content=content,
            )
            version = await write_version(
                session,
                case,
                content,
                report,
                source="ai",
                change_reason="created",
                created_by=None,
            )
            case.original_version_id = version.id
            needing_attention += 0 if report.ready else 1
            created.append(case)

        suite_id = await self._suite_for(session, instruction, plan, created)
        return {
            "generated": len(created),
            "duplicates_removed": duplicates + skipped_existing,
            "needs_attention": needing_attention,
            "suite_id": str(suite_id) if suite_id else None,
        }

    async def _store_rewrites(
        self,
        session: AsyncSession,
        instruction: TestInstruction,
        rewrite_cases: list[tuple[uuid.UUID, CaseContent]],
        generated: list[GeneratedCase],
    ) -> dict[str, Any]:
        org = instruction.organization_id
        cases = TestCaseRepository(session, org)
        rewritten = 0
        # The model returns one case per requested case, in the same order.
        for (case_id, _old), item in zip(rewrite_cases, generated, strict=False):
            case = await cases.get(case_id)
            if case is None:
                continue
            content = CaseContent.model_validate(item.model_dump(exclude={"local_id"}))
            facts = await build_facts(
                session,
                organization_id=org,
                project_id=instruction.project_id,
                environment_id=case.environment_id,
                instruction_id=instruction.id,
                exclude_case_id=case.id,
            )
            await write_version(
                session,
                case,
                content,
                validate_case(content, facts),
                source="ai",
                change_reason="regenerated",
                created_by=None,
            )
            rewritten += 1
        return {"regenerated": rewritten, "requested": len(rewrite_cases)}

    async def _suite_for(
        self,
        session: AsyncSession,
        instruction: TestInstruction,
        plan: TestPlan | None,
        created: list[TestCase],
    ) -> uuid.UUID | None:
        """Put the new cases into the instruction's draft suite, creating it if needed."""
        if not created:
            return None
        org = instruction.organization_id
        suites = SuiteRepository(session, org)
        existing = [
            s
            for s in await suites.list_in_project(instruction.project_id, feature=None, status=None)
            if s.instruction_id == instruction.id
        ]
        feature = str((plan.scope if plan else {}).get("feature_name") or "Generated tests")
        if existing:
            suite = existing[0]
        else:
            suite = await suites.add(
                TestSuite(
                    organization_id=org,
                    project_id=instruction.project_id,
                    application_id=instruction.application_id,
                    name=f"{feature} ({str(instruction.id)[-6:]})",
                    description=plan.summary if plan else None,
                    feature=feature,
                    source="ai_generated",
                    instruction_id=instruction.id,
                    status="draft",
                )
            )
        members = (await suites.members([suite.id]))[suite.id]
        await suites.replace_members(
            suite.id, [m.test_case_id for m in members] + [c.id for c in created]
        )
        await suites.reopen([suite.id])
        return suite.id

    # --- claiming ----------------------------------------------------------------

    async def _claim(
        self,
        organization_id: uuid.UUID,
        instruction_id: uuid.UUID,
        run_token: str,
        case_ids: Sequence[uuid.UUID],
    ) -> (
        tuple[
            dict[str, Any], GenerationContext, list[str], list[tuple[uuid.UUID, CaseContent]], str
        ]
        | None
    ):
        # The job may be picked up before the request that enqueued it has committed.
        for attempt in range(8):
            async with self._session_factory() as session, session.begin():
                await set_current_org(session, organization_id)
                instruction = await InstructionRepository(session, organization_id).get_for_update(
                    instruction_id
                )
                if (
                    instruction is not None
                    and instruction.status == "generating"
                    and instruction.run_token == run_token
                ):
                    plan = await PlanRepository(session, organization_id).latest(instruction_id)
                    if plan is None:
                        return None
                    context = await self._context(session, instruction)
                    existing = await TestCaseRepository(session, organization_id).list_in_project(
                        instruction.project_id, instruction_id=instruction.id
                    )
                    views = await load_views(session, organization_id, existing)
                    wanted = set(case_ids)
                    rewrite = [(v.case.id, v.content) for v in views if v.case.id in wanted]
                    titles = [v.content.title for v in views if v.case.id not in wanted]
                    previous = str(
                        (instruction.generation or {}).get("previous_status", "approved")
                    )
                    return dict(plan.scope), context, titles, rewrite, previous
            if attempt < 7:
                await asyncio.sleep(0.25)
        return None

    @staticmethod
    async def _context(session: AsyncSession, instruction: TestInstruction) -> GenerationContext:
        from app.services.instructions import load_target

        _, _, _, analysis_context = await load_target(session, instruction)
        options = InstructionOptions.model_validate(instruction.options or {})
        return GenerationContext(
            application=analysis_context.application,
            environment=analysis_context.environment,
            available_credential_roles=analysis_context.available_credential_roles,
            configured_rules=[rule.model_dump(exclude_none=True) for rule in options.rules],
            requested_test_types=list(instruction.test_types),
        )
