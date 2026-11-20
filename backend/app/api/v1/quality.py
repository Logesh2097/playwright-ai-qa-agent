"""Failure reports, replacement locators found by the engine, and test stability."""

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query

from app.api.deps import ProjectContextDep, get_execution_service, get_quality_service
from app.models import LocatorHealProposal
from app.schemas.common import ProblemDetail
from app.schemas.executions import (
    FailureReportResponse,
    HealDecisionResponse,
    HealProposalResponse,
    StabilityResponse,
)
from app.services.executions import ExecutionService
from app.services.quality import CaseStability, QualityService

router = APIRouter(
    prefix="/organizations/{organization_id}/projects/{project_id}", tags=["quality"]
)
Quality = Annotated[QualityService, Depends(get_quality_service)]
Executions = Annotated[ExecutionService, Depends(get_execution_service)]

_ERR: dict[str, Any] = {"model": ProblemDetail}
_READ: dict[int | str, dict[str, Any]] = {401: _ERR, 404: _ERR}
_WRITE: dict[int | str, dict[str, Any]] = {401: _ERR, 403: _ERR, 404: _ERR, 409: _ERR}


def _proposal(row: LocatorHealProposal) -> HealProposalResponse:
    return HealProposalResponse(
        id=row.id,
        test_case_id=row.test_case_id,
        test_case_version_id=row.test_case_version_id,
        result_id=row.result_id,
        script_phase=row.script_phase,
        script_index=row.script_index,
        action=row.action,
        old_locator=row.old_locator,
        new_locator=row.new_locator,
        confidence=float(row.confidence) if row.confidence is not None else None,
        rationale=row.rationale,
        held_back=row.held_back,
        status=row.status,
        verified_by_rerun=row.verified_by_rerun,
        decided_by=row.decided_by,
        decided_at=row.decided_at,
        created_at=row.created_at,
    )


def _stability(item: CaseStability) -> StabilityResponse:
    return StabilityResponse(
        case_id=item.case.id,
        case_key=item.case.key,
        title=item.case.title,
        **item.stability.as_dict(),
    )


@router.get(
    "/executions/{execution_id}/results/{result_id}/failure",
    summary="What was recorded about a result that did not pass, and any AI suggestion",
    responses=_READ,
)
async def failure_report(
    execution_id: uuid.UUID,
    result_id: uuid.UUID,
    ctx: ProjectContextDep,
    executions: Executions,
    quality: Quality,
) -> FailureReportResponse:
    execution = await executions.get(ctx, execution_id)
    report = await quality.failure_report(ctx, execution, result_id)
    return FailureReportResponse(record=report.record, ai_suggestion=report.suggestion)


@router.post(
    "/executions/{execution_id}/results/{result_id}/failure/analysis",
    summary="Ask the AI for its reading of a failure (a suggestion; changes no result)",
    responses={**_WRITE, 503: _ERR},
)
async def analyse_failure(
    execution_id: uuid.UUID,
    result_id: uuid.UUID,
    ctx: ProjectContextDep,
    executions: Executions,
    quality: Quality,
) -> FailureReportResponse:
    execution = await executions.get(ctx, execution_id)
    report = await quality.analyse_failure(ctx, execution, result_id)
    return FailureReportResponse(record=report.record, ai_suggestion=report.suggestion)


@router.get(
    "/heal-proposals",
    summary="Replacement locators the engine found for locators that no longer match",
    responses=_READ,
)
async def list_heal_proposals(
    ctx: ProjectContextDep,
    quality: Quality,
    status: Annotated[
        Literal["proposed", "verified", "accepted", "rejected"] | None, Query()
    ] = None,
    case_id: Annotated[uuid.UUID | None, Query()] = None,
) -> list[HealProposalResponse]:
    rows = await quality.heal_proposals(ctx, status=status, case_id=case_id)
    return [_proposal(row) for row in rows]


@router.post(
    "/heal-proposals/{proposal_id}/accept",
    summary="Use the replacement: a new case version that needs approval",
    responses=_WRITE,
)
async def accept_heal_proposal(
    proposal_id: uuid.UUID, ctx: ProjectContextDep, quality: Quality
) -> HealDecisionResponse:
    proposal, case = await quality.accept_proposal(ctx, proposal_id)
    return HealDecisionResponse(proposal=_proposal(proposal), case_status=case.status)


@router.post(
    "/heal-proposals/{proposal_id}/reject",
    summary="Discard the replacement; the case is left as it is",
    responses=_WRITE,
)
async def reject_heal_proposal(
    proposal_id: uuid.UUID, ctx: ProjectContextDep, quality: Quality
) -> HealDecisionResponse:
    return HealDecisionResponse(proposal=_proposal(await quality.reject_proposal(ctx, proposal_id)))


@router.get(
    "/stability",
    summary="How steady each case's results have been; flaky cases first",
    responses=_READ,
)
async def list_stability(
    ctx: ProjectContextDep,
    quality: Quality,
    flaky_only: Annotated[bool, Query()] = False,
    window: Annotated[int, Query(ge=3, le=100)] = 20,
) -> list[StabilityResponse]:
    found = await quality.stability(ctx, window=window, only_flaky=flaky_only)
    return [_stability(item) for item in found]


@router.get(
    "/test-cases/{case_id}/stability",
    summary="How steady one case's results have been",
    tags=["test cases"],
    responses=_READ,
)
async def case_stability(
    case_id: uuid.UUID,
    ctx: ProjectContextDep,
    quality: Quality,
    window: Annotated[int, Query(ge=3, le=100)] = 20,
) -> StabilityResponse:
    return _stability((await quality.stability(ctx, case_id=case_id, window=window))[0])
