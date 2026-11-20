"""What people do with results: read failure reports, decide on replacement locators,
and see which tests are unstable.

Nothing here changes a recorded result or a verdict.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.llm.base import LLMError, LLMGateway
from app.agent.nodes.analyse_failures import explain_failures
from app.core.errors import ConflictError, NotFoundError, ServiceUnavailableError
from app.core.security.permissions import Permission
from app.models import LocatorHealProposal, TestCase, TestExecution, TestResult
from app.repositories.executions import (
    EvidenceRepository,
    HealProposalRepository,
    ResultRepository,
    StepRepository,
)
from app.repositories.test_cases import CaseVersionRepository, TestCaseRepository
from app.services.audit import AuditEvent, AuditService
from app.services.automation import parse_script
from app.services.failure_analysis import (
    EXPLAINABLE,
    as_suggestion,
    facts_for_model,
    failure_record,
)
from app.services.projects import ProjectContext
from app.services.test_cases import TestCaseService
from app.testing.automation import Locator, Script
from app.testing.flakiness import DEFAULT_WINDOW, RunRecord, Stability, analyse

OPEN_STATUSES = ("proposed", "verified")


@dataclass(frozen=True, slots=True)
class FailureReport:
    record: dict[str, Any]
    suggestion: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class CaseStability:
    case: TestCase
    stability: Stability


class QualityService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        audit: AuditService,
        gateway: LLMGateway,
        cases: TestCaseService,
    ) -> None:
        self._session = session
        self._audit = audit
        self._gateway = gateway
        self._cases = cases

    # --- failure reports ---------------------------------------------------------------

    async def _result(
        self, ctx: ProjectContext, execution: TestExecution, result_id: uuid.UUID
    ) -> TestResult:
        result = await ResultRepository(self._session, ctx.organization_id).get_in_execution(
            execution.id, result_id
        )
        if result is None:
            raise NotFoundError("Result not found.")
        return result

    async def _record(
        self, ctx: ProjectContext, execution: TestExecution, result: TestResult
    ) -> dict[str, Any]:
        organization = ctx.organization_id
        steps = await StepRepository(self._session, organization).for_result(result.id)
        evidence = await EvidenceRepository(self._session, organization).for_execution(execution.id)
        cases = await TestCaseRepository(self._session, organization).by_ids_any(
            [result.test_case_id]
        )
        version = await CaseVersionRepository(self._session, organization).get(
            result.test_case_version_id
        )
        case = cases.get(result.test_case_id)
        return failure_record(
            result,
            steps,
            evidence,
            case_key=case.key if case else str(result.test_case_id),
            title=version.title if version else "",
            expected_result=version.expected_result if version else None,
            execution_error=(execution.error or {}).get("code"),
        )

    async def failure_report(
        self, ctx: ProjectContext, execution: TestExecution, result_id: uuid.UUID
    ) -> FailureReport:
        ctx.require(Permission.PROJECTS_READ)
        result = await self._result(ctx, execution, result_id)
        return FailureReport(await self._record(ctx, execution, result), result.ai_analysis)

    async def analyse_failure(
        self, ctx: ProjectContext, execution: TestExecution, result_id: uuid.UUID
    ) -> FailureReport:
        """Ask a model for its reading of one result. Stored as a suggestion; changes nothing else."""
        ctx.require(Permission.EXECUTIONS_RUN)
        ctx.require_writable()
        result = await self._result(ctx, execution, result_id)
        if result.status not in EXPLAINABLE:
            raise ConflictError("Only results that failed or ended in an error are analysed.")
        if not self._gateway.configured:
            raise ServiceUnavailableError("The AI service is not configured on this installation.")
        record = await self._record(ctx, execution, result)
        try:
            answer = await explain_failures(self._gateway, [facts_for_model("R1", record)])
        except LLMError as exc:
            raise ServiceUnavailableError(f"No analysis could be obtained: {exc}") from exc
        explanation = next((e for e in answer.output.explanations if e.ref == "R1"), None)
        if explanation is None:
            raise ServiceUnavailableError("The AI service gave no analysis for this result.")
        before = (result.status, result.failure_category)
        result.ai_analysis = as_suggestion(
            explanation,
            category=(record["classification"] or {}).get("category"),
            model=answer.model,
        )
        assert (result.status, result.failure_category) == before  # noqa: S101
        await self._audit.record(
            self._session,
            AuditEvent(
                action="test_result.analyse",
                actor_id=ctx.user.id,
                organization_id=ctx.organization_id,
                resource_type="test_result",
                resource_id=result.id,
                changes={"model": answer.model},
            ),
        )
        return FailureReport(record, result.ai_analysis)

    # --- replacement locators ----------------------------------------------------------

    async def _project_case_ids(self, ctx: ProjectContext) -> list[uuid.UUID]:
        cases = await TestCaseRepository(self._session, ctx.organization_id).list_in_project(
            ctx.project.id, limit=5000
        )
        return [case.id for case in cases]

    async def heal_proposals(
        self, ctx: ProjectContext, *, status: str | None = None, case_id: uuid.UUID | None = None
    ) -> Sequence[LocatorHealProposal]:
        ctx.require(Permission.PROJECTS_READ)
        ids = await self._project_case_ids(ctx)
        if case_id is not None:
            ids = [i for i in ids if i == case_id]
        return await HealProposalRepository(self._session, ctx.organization_id).list_for_cases(
            ids, status=status
        )

    async def _open_proposal(
        self, ctx: ProjectContext, proposal_id: uuid.UUID
    ) -> tuple[LocatorHealProposal, TestCase]:
        proposal = await HealProposalRepository(self._session, ctx.organization_id).get_for_update(
            proposal_id
        )
        if proposal is None or proposal.test_case_id is None:
            raise NotFoundError("Proposal not found.")
        # Raises NotFound for a case of another project.
        case = await self._cases.get(ctx, proposal.test_case_id)
        if proposal.status not in OPEN_STATUSES:
            raise ConflictError(f"This proposal was already {proposal.status}.")
        return proposal, case

    async def accept_proposal(
        self, ctx: ProjectContext, proposal_id: uuid.UUID
    ) -> tuple[LocatorHealProposal, TestCase]:
        """Put the replacement locator into the case's script, as a new version.

        Only the one locator changes. The new version is `needs_review`: it has to be
        approved before it runs again, so a replacement is never approved by a machine.
        """
        ctx.require(Permission.CASES_MANAGE)
        ctx.require_writable()
        proposal, case = await self._open_proposal(ctx, proposal_id)
        view = await self._cases.view(ctx, case)
        script, _ = parse_script(view.version.automation)
        steps = None
        if script is not None:
            steps = script.cleanup if proposal.script_phase == "cleanup" else script.steps
        index = proposal.script_index
        if script is None or steps is None or index is None or not 0 <= index < len(steps):
            raise ConflictError(
                "The step this replacement was found for is no longer in the script."
            )
        step = steps[index]
        current = getattr(step, "locator", None)
        old = Locator.model_validate(proposal.old_locator)
        if current is None or step.action != proposal.action or current != old:
            # The script has changed since the replacement was found. Guessing which
            # step it now belongs to could put the locator on a different action.
            raise ConflictError(
                "The test case has changed since this replacement was found: the step no "
                "longer uses that locator. Run it again; a new proposal is made if it still fails."
            )
        replacement = Locator.model_validate(proposal.new_locator)
        changed = list(steps)
        changed[index] = step.model_copy(update={"locator": replacement})
        field = "cleanup" if proposal.script_phase == "cleanup" else "steps"
        # Validated again as a whole: nothing but the locator differs.
        healed = Script.model_validate(
            {
                **script.model_dump(mode="json", exclude_none=True),
                field: [s.model_dump(mode="json", exclude_none=True) for s in changed],
            }
        )
        await self._cases.set_automation(ctx, case, healed, change_reason="healed")
        await self._decide(ctx, proposal, "accepted")
        return proposal, case

    async def reject_proposal(
        self, ctx: ProjectContext, proposal_id: uuid.UUID
    ) -> LocatorHealProposal:
        ctx.require(Permission.CASES_MANAGE)
        ctx.require_writable()
        proposal, _ = await self._open_proposal(ctx, proposal_id)
        await self._decide(ctx, proposal, "rejected")
        return proposal

    async def _decide(
        self, ctx: ProjectContext, proposal: LocatorHealProposal, status: str
    ) -> None:
        proposal.status = status
        proposal.decided_by = ctx.user.id
        proposal.decided_at = datetime.now(UTC)
        await self._audit.record(
            self._session,
            AuditEvent(
                action=f"heal_proposal.{status}",
                actor_id=ctx.user.id,
                organization_id=ctx.organization_id,
                resource_type="locator_heal_proposal",
                resource_id=proposal.id,
                changes={
                    "test_case_id": str(proposal.test_case_id),
                    "old_locator": proposal.old_locator,
                    "new_locator": proposal.new_locator,
                    "verified_by_rerun": proposal.verified_by_rerun,
                },
            ),
        )
        await self._session.flush()

    # --- stability ---------------------------------------------------------------------

    async def stability(
        self,
        ctx: ProjectContext,
        *,
        case_id: uuid.UUID | None = None,
        window: int = DEFAULT_WINDOW,
        only_flaky: bool = False,
    ) -> list[CaseStability]:
        """How steady each case's results have been. Read-only: nothing is hidden or re-labelled."""
        ctx.require(Permission.PROJECTS_READ)
        cases = await TestCaseRepository(self._session, ctx.organization_id).list_in_project(
            ctx.project.id, limit=5000
        )
        wanted = {case.id: case for case in cases if case_id is None or case.id == case_id}
        if case_id is not None and not wanted:
            raise NotFoundError("Test case not found.")
        rows = await ResultRepository(self._session, ctx.organization_id).history(
            ctx.project.id, case_ids=list(wanted) if case_id else None
        )
        history: dict[uuid.UUID, list[RunRecord]] = {}
        # Oldest first, so that results finished at the same instant keep their order.
        for result, environment_id in reversed(rows):
            if result.test_case_id not in wanted or result.finished_at is None:
                continue
            history.setdefault(result.test_case_id, []).append(
                RunRecord(
                    finished_at=result.finished_at,
                    status=result.status,
                    version_id=result.test_case_version_id,
                    environment_id=environment_id,
                    failure_category=result.failure_category,
                    failure_class=result.failure_class,
                    healed=result.healed,
                    attempt_count=result.attempt_count,
                )
            )
        found = [
            CaseStability(wanted[key], analyse(records, window=window))
            for key, records in history.items()
        ]
        if case_id is not None and not found:
            found = [CaseStability(wanted[case_id], analyse([], window=window))]
        if only_flaky:
            found = [item for item in found if item.stability.is_flaky]
        return sorted(
            found,
            key=lambda item: (
                not item.stability.is_flaky,
                -item.stability.flake_rate,
                item.case.key,
            ),
        )
