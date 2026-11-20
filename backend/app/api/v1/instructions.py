"""Test instructions and AI requirement analysis.

Submitting an instruction returns immediately with status `analysing`; the
analysis runs in the background worker. Poll the instruction until its status
changes to `analysed`, `needs_clarification` or `failed`.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.agent.schemas.requirement import ClarificationQuestion, RequirementAnalysis
from app.api.deps import InstructionServiceDep, ProjectContextDep
from app.models import TestInstruction, TestPlan
from app.repositories.instructions import UsageTotals
from app.schemas.common import ProblemDetail
from app.schemas.instructions import (
    AnsweredQuestion,
    ClarificationSubmit,
    InstructionCreate,
    InstructionError,
    InstructionOptions,
    InstructionResponse,
    InstructionStatus,
    InstructionUpdate,
    PlanResponse,
    UsageSummary,
    ValidationResult,
)

router = APIRouter(
    prefix="/organizations/{organization_id}/projects/{project_id}/instructions",
    tags=["instructions"],
)

Responses = dict[int | str, dict[str, Any]]
_ERR: dict[str, Any] = {"model": ProblemDetail}
_READ: Responses = {401: _ERR, 404: _ERR}
_WRITE: Responses = {401: _ERR, 403: _ERR, 404: _ERR, 409: _ERR, 422: _ERR, 429: _ERR, 503: _ERR}


async def get_instruction(
    instruction_id: uuid.UUID, ctx: ProjectContextDep, service: InstructionServiceDep
) -> TestInstruction:
    return await service.get(ctx, instruction_id)


InstructionDep = Annotated[TestInstruction, Depends(get_instruction)]


def _response(instruction: TestInstruction, usage: UsageTotals) -> InstructionResponse:
    return InstructionResponse(
        id=instruction.id,
        project_id=instruction.project_id,
        application_id=instruction.application_id,
        environment_id=instruction.environment_id,
        created_by=instruction.created_by,
        text=instruction.raw_text,
        test_types=list(instruction.test_types),
        options=InstructionOptions.model_validate(instruction.options or {}),
        status=instruction.status,
        analysis=(
            RequirementAnalysis.model_validate(instruction.analysis)
            if instruction.analysis
            else None
        ),
        analysis_source=instruction.analysis_source,
        analysis_model=instruction.analysis_model,
        analysis_prompt=instruction.analysis_prompt,
        analysed_at=instruction.analysed_at,
        validation=(
            ValidationResult.model_validate(instruction.validation)
            if instruction.validation
            else None
        ),
        clarification_questions=[
            ClarificationQuestion.model_validate(q)
            for q in instruction.clarification_questions or []
        ],
        clarification_answers=[
            AnsweredQuestion.model_validate(a) for a in instruction.clarification_answers or []
        ],
        error=InstructionError.model_validate(instruction.error) if instruction.error else None,
        generation=instruction.generation,
        usage=UsageSummary(
            llm_calls=usage.calls,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.cost_usd,
        ),
        created_at=instruction.created_at,
        updated_at=instruction.updated_at,
    )


def _plan(plan: TestPlan) -> PlanResponse:
    return PlanResponse(
        id=plan.id,
        instruction_id=plan.instruction_id,
        version=plan.version,
        status=plan.status,
        summary=plan.summary,
        scope=plan.scope,
        approved_by=plan.approved_by,
        approved_at=plan.approved_at,
        created_at=plan.created_at,
    )


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a testing instruction",
    responses=_WRITE,
)
async def submit_instruction(
    data: InstructionCreate, ctx: ProjectContextDep, service: InstructionServiceDep
) -> InstructionResponse:
    """Stores the instruction and starts its analysis. The application and environment
    are the ones named here; the AI cannot choose or change them."""
    instruction = await service.submit(ctx, data)
    return _response(instruction, await service.usage(ctx, instruction))


@router.get("", summary="List instructions, newest first", responses=_READ)
async def list_instructions(
    ctx: ProjectContextDep,
    service: InstructionServiceDep,
    status_filter: Annotated[InstructionStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[InstructionResponse]:
    return [
        _response(instruction, await service.usage(ctx, instruction))
        for instruction in await service.list(ctx, status=status_filter, limit=limit)
    ]


@router.get(
    "/{instruction_id}", summary="Retrieve an instruction and its analysis", responses=_READ
)
async def get_instruction_route(
    ctx: ProjectContextDep, instruction: InstructionDep, service: InstructionServiceDep
) -> InstructionResponse:
    """`analysis` is the structured requirement. `validation` says whether it is ready
    and why not; `clarification_questions` lists what still has to be answered."""
    return _response(instruction, await service.usage(ctx, instruction))


@router.patch(
    "/{instruction_id}",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Change the instruction and analyse it again",
    responses=_WRITE,
)
async def update_instruction(
    data: InstructionUpdate,
    ctx: ProjectContextDep,
    instruction: InstructionDep,
    service: InstructionServiceDep,
) -> InstructionResponse:
    instruction = await service.update(ctx, instruction, data)
    return _response(instruction, await service.usage(ctx, instruction))


@router.post(
    "/{instruction_id}/analyse",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Run the analysis again",
    responses=_WRITE,
)
async def analyse_instruction(
    ctx: ProjectContextDep, instruction: InstructionDep, service: InstructionServiceDep
) -> InstructionResponse:
    """For a failed analysis, or to get a fresh one. Limited per instruction."""
    instruction = await service.reanalyse(ctx, instruction)
    return _response(instruction, await service.usage(ctx, instruction))


@router.post(
    "/{instruction_id}/clarifications",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Answer clarification questions",
    responses=_WRITE,
)
async def submit_clarification(
    data: ClarificationSubmit,
    ctx: ProjectContextDep,
    instruction: InstructionDep,
    service: InstructionServiceDep,
) -> InstructionResponse:
    """The answers are added to the instruction and it is analysed again."""
    instruction = await service.clarify(ctx, instruction, data.answers)
    return _response(instruction, await service.usage(ctx, instruction))


@router.put(
    "/{instruction_id}/analysis",
    summary="Replace the structured requirement by hand",
    responses=_WRITE,
)
async def edit_analysis(
    analysis: RequirementAnalysis,
    ctx: ProjectContextDep,
    instruction: InstructionDep,
    service: InstructionServiceDep,
) -> InstructionResponse:
    """The submitted requirement goes through the same schema and the same checks as
    one written by the AI. It is marked `human_edited`."""
    instruction = await service.edit_analysis(ctx, instruction, analysis)
    return _response(instruction, await service.usage(ctx, instruction))


@router.post(
    "/{instruction_id}/validate",
    summary="Repeat the readiness checks without calling the AI",
    responses=_WRITE,
)
async def validate_instruction(
    ctx: ProjectContextDep, instruction: InstructionDep, service: InstructionServiceDep
) -> InstructionResponse:
    """Use after fixing something the checks complained about, such as adding a credential."""
    instruction = await service.revalidate(ctx, instruction)
    return _response(instruction, await service.usage(ctx, instruction))


@router.post(
    "/{instruction_id}/approve",
    status_code=status.HTTP_201_CREATED,
    summary="Approve the requirement as the testing plan (QA engineer, admin, owner)",
    responses=_WRITE,
)
async def approve_instruction(
    ctx: ProjectContextDep, instruction: InstructionDep, service: InstructionServiceDep
) -> PlanResponse:
    """Possible only when `validation.ready` is true. Production needs an admin."""
    return _plan(await service.approve(ctx, instruction))


@router.get("/{instruction_id}/plan", summary="The current testing plan", responses=_READ)
async def get_plan(
    ctx: ProjectContextDep, instruction: InstructionDep, service: InstructionServiceDep
) -> PlanResponse:
    return _plan(await service.plan(ctx, instruction))


@router.post("/{instruction_id}/cancel", summary="Cancel an instruction", responses=_WRITE)
async def cancel_instruction(
    ctx: ProjectContextDep, instruction: InstructionDep, service: InstructionServiceDep
) -> InstructionResponse:
    instruction = await service.cancel(ctx, instruction)
    return _response(instruction, await service.usage(ctx, instruction))
