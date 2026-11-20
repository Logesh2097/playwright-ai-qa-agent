"""The agent workflow: from an instruction to results, with a person in the loop."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, status

from app.api.deps import ProjectContextDep, get_workflow_service
from app.core.errors import NotFoundError
from app.models import WorkflowRun
from app.schemas.common import ProblemDetail
from app.schemas.workflows import (
    ApprovalDecision,
    ClarificationSubmit,
    NodeEvent,
    WorkflowCreate,
    WorkflowResponse,
    WorkflowSummary,
)
from app.services.workflows import WorkflowService

router = APIRouter(
    prefix="/organizations/{organization_id}/projects/{project_id}/workflows", tags=["workflows"]
)
Service = Annotated[WorkflowService, Depends(get_workflow_service)]
_ERR: dict[str, Any] = {"model": ProblemDetail}
_READ: dict[int | str, dict[str, Any]] = {401: _ERR, 404: _ERR}
_WRITE: dict[int | str, dict[str, Any]] = {401: _ERR, 403: _ERR, 404: _ERR, 409: _ERR, 422: _ERR}


def _summary(run: WorkflowRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "mode": run.mode,
        "status": run.status,
        "verdict": run.verdict,
        "current_node": run.current_node,
        "environment_id": run.environment_id,
        "instruction_id": run.instruction_id,
        "suite_id": run.suite_id,
        "execution_ids": run.execution_ids,
        "retry_count": run.retry_count,
        "started_by": run.started_by,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


async def _response(
    ctx: ProjectContextDep, service: WorkflowService, run: WorkflowRun
) -> WorkflowResponse:
    usage, nodes = await service.trace(ctx, run)
    return WorkflowResponse.model_validate(
        {
            **_summary(run),
            "options": run.options,
            "waiting_for": run.pending,
            "error": run.error,
            "usage": usage,
            "history": [NodeEvent.model_validate(node) for node in nodes],
            "has_report": run.report is not None,
        }
    )


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start the agent workflow from an instruction, or for an approved suite",
    responses={**_WRITE, 429: _ERR, 503: _ERR},
)
async def start_workflow(
    body: WorkflowCreate, ctx: ProjectContextDep, service: Service
) -> WorkflowResponse:
    """Runs in the background. Poll the run: it will ask for answers or approval when it needs them."""
    return await _response(ctx, service, await service.start(ctx, body))


@router.get("", summary="List workflow runs, newest first", responses=_READ)
async def list_workflows(ctx: ProjectContextDep, service: Service) -> list[WorkflowSummary]:
    return [WorkflowSummary.model_validate(_summary(run)) for run in await service.list_runs(ctx)]


@router.get(
    "/{run_id}",
    summary="A workflow run: status, what it waits for, history, usage",
    responses=_READ,
)
async def get_workflow(
    run_id: uuid.UUID, ctx: ProjectContextDep, service: Service
) -> WorkflowResponse:
    return await _response(ctx, service, await service.get(ctx, run_id))


@router.post(
    "/{run_id}/clarifications", summary="Answer the run's open questions", responses=_WRITE
)
async def answer_questions(
    run_id: uuid.UUID, body: ClarificationSubmit, ctx: ProjectContextDep, service: Service
) -> WorkflowResponse:
    return await _response(ctx, service, await service.clarify(ctx, run_id, body.answers))


@router.post(
    "/{run_id}/approval", summary="Approve or reject what the run is waiting for", responses=_WRITE
)
async def decide_approval(
    run_id: uuid.UUID, body: ApprovalDecision, ctx: ProjectContextDep, service: Service
) -> WorkflowResponse:
    """QA engineers and admins. Runs that change data on an approval-required
    environment, or target production, need an organization admin."""
    run = await service.decide(ctx, run_id, approve=body.decision == "approve", note=body.note)
    return await _response(ctx, service, run)


@router.post("/{run_id}/cancel", summary="Stop a workflow run", responses=_WRITE)
async def cancel_workflow(
    run_id: uuid.UUID, ctx: ProjectContextDep, service: Service
) -> WorkflowResponse:
    return await _response(ctx, service, await service.cancel(ctx, run_id))


@router.post(
    "/{run_id}/recover",
    summary="Continue a run that was interrupted, from its last checkpoint",
    responses=_WRITE,
)
async def recover_workflow(
    run_id: uuid.UUID, ctx: ProjectContextDep, service: Service
) -> WorkflowResponse:
    return await _response(ctx, service, await service.recover(ctx, run_id))


@router.get("/{run_id}/report", summary="The run's final report", responses=_READ)
async def get_report(run_id: uuid.UUID, ctx: ProjectContextDep, service: Service) -> dict[str, Any]:
    run = await service.get(ctx, run_id)
    if run.report is None:
        raise NotFoundError("This run has no report yet.")
    return run.report
