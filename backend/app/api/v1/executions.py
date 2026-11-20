"""Test execution: scripts on cases, test data, executions, results and evidence."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import (
    ProjectContextDep,
    get_execution_service,
    get_test_case_service,
    get_test_data_service,
)
from app.models import TestDataSet, TestEvidence, TestExecution, TestFile
from app.schemas.common import ProblemDetail
from app.schemas.executions import (
    AutomationResponse,
    DataSetResponse,
    DataSetWrite,
    EvidenceResponse,
    ExecutionCreate,
    ExecutionResponse,
    ExecutionSummary,
    ResultResponse,
    StepResponse,
    TestFileCreate,
    TestFileResponse,
)
from app.schemas.test_cases import CaseValidation
from app.services.automation import parse_script
from app.services.executions import ExecutionService, ExecutionView
from app.services.test_cases import TestCaseService
from app.services.test_data import TestDataService
from app.testing.automation import Script as AutomationScript

router = APIRouter(
    prefix="/organizations/{organization_id}/projects/{project_id}", tags=["executions"]
)

Executions = Annotated[ExecutionService, Depends(get_execution_service)]
Cases = Annotated[TestCaseService, Depends(get_test_case_service)]
Data = Annotated[TestDataService, Depends(get_test_data_service)]
_ERR: dict[str, Any] = {"model": ProblemDetail}
_READ: dict[int | str, dict[str, Any]] = {401: _ERR, 404: _ERR}
_WRITE: dict[int | str, dict[str, Any]] = {401: _ERR, 403: _ERR, 404: _ERR, 409: _ERR, 422: _ERR}


# --- scripts ------------------------------------------------------------------------------


async def _automation(
    ctx: ProjectContextDep, cases: TestCaseService, case_id: uuid.UUID
) -> AutomationResponse:
    case = await cases.get(ctx, case_id)
    view = await cases.view(ctx, case)
    script, _ = parse_script(view.version.automation)
    return AutomationResponse(
        case_id=case.id,
        key=case.key,
        version=view.version.version,
        status=case.status,
        script=script,
        validation=CaseValidation.model_validate(
            view.version.validation or {"ready": False, "issues": []}
        ),
    )


@router.get(
    "/test-cases/{case_id}/automation",
    summary="The executable script of a case",
    tags=["test cases"],
    responses=_READ,
)
async def get_automation(
    case_id: uuid.UUID, ctx: ProjectContextDep, cases: Cases
) -> AutomationResponse:
    return await _automation(ctx, cases, case_id)


@router.put(
    "/test-cases/{case_id}/automation",
    summary="Attach or replace the executable script of a case",
    tags=["test cases"],
    responses=_WRITE,
)
async def put_automation(
    case_id: uuid.UUID, script: AutomationScript, ctx: ProjectContextDep, cases: Cases
) -> AutomationResponse:
    """Saved as a new version of the case, which then needs approval again.

    Problems that can be found without a browser are returned in `validation`;
    blocking ones prevent approval.
    """
    await cases.set_automation(ctx, await cases.get(ctx, case_id), script)
    return await _automation(ctx, cases, case_id)


@router.delete(
    "/test-cases/{case_id}/automation",
    summary="Remove the script of a case",
    tags=["test cases"],
    responses=_WRITE,
)
async def delete_automation(
    case_id: uuid.UUID, ctx: ProjectContextDep, cases: Cases
) -> AutomationResponse:
    await cases.set_automation(ctx, await cases.get(ctx, case_id), None)
    return await _automation(ctx, cases, case_id)


# --- test data ----------------------------------------------------------------------------


def _data_set(item: TestDataSet) -> DataSetResponse:
    return DataSetResponse(
        id=item.id,
        name=item.name,
        environment_id=item.environment_id,
        description=item.description,
        values={str(k): str(v) for k, v in item.values.items()},
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _file(item: TestFile) -> TestFileResponse:
    return TestFileResponse(
        id=item.id,
        name=item.name,
        content_type=item.content_type,
        size_bytes=item.size_bytes,
        sha256=item.sha256,
        created_at=item.created_at,
    )


@router.get("/test-data-sets", summary="List test data sets", tags=["test data"], responses=_READ)
async def list_data_sets(ctx: ProjectContextDep, data: Data) -> list[DataSetResponse]:
    return [_data_set(item) for item in await data.list_sets(ctx)]


@router.post(
    "/test-data-sets",
    status_code=status.HTTP_201_CREATED,
    summary="Create a test data set",
    tags=["test data"],
    responses=_WRITE,
)
async def create_data_set(
    body: DataSetWrite, ctx: ProjectContextDep, data: Data
) -> DataSetResponse:
    return _data_set(await data.create_set(ctx, body))


@router.put(
    "/test-data-sets/{set_id}",
    summary="Replace the values of a test data set",
    tags=["test data"],
    responses=_WRITE,
)
async def update_data_set(
    set_id: uuid.UUID, body: DataSetWrite, ctx: ProjectContextDep, data: Data
) -> DataSetResponse:
    return _data_set(await data.update_set(ctx, await data.get_set(ctx, set_id), body))


@router.delete(
    "/test-data-sets/{set_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a test data set",
    tags=["test data"],
    responses=_WRITE,
)
async def delete_data_set(set_id: uuid.UUID, ctx: ProjectContextDep, data: Data) -> Response:
    await data.delete_set(ctx, await data.get_set(ctx, set_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/test-files", summary="List test files", tags=["test data"], responses=_READ)
async def list_files(ctx: ProjectContextDep, data: Data) -> list[TestFileResponse]:
    return [_file(item) for item in await data.list_files(ctx)]


@router.post(
    "/test-files",
    status_code=status.HTTP_201_CREATED,
    summary="Add a file that tests may upload",
    tags=["test data"],
    responses=_WRITE,
)
async def create_file(body: TestFileCreate, ctx: ProjectContextDep, data: Data) -> TestFileResponse:
    """QA engineers and admins only. The content is never returned by the API."""
    return _file(await data.create_file(ctx, body))


@router.delete(
    "/test-files/{file_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a test file",
    tags=["test data"],
    responses=_WRITE,
)
async def delete_file(file_id: uuid.UUID, ctx: ProjectContextDep, data: Data) -> Response:
    await data.delete_file(ctx, file_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- executions ---------------------------------------------------------------------------


def _summary(execution: TestExecution) -> dict[str, Any]:
    return {
        "id": execution.id,
        "environment_id": execution.environment_id,
        "suite_id": execution.suite_id,
        "status": execution.status,
        "verdict": execution.verdict,
        "totals": execution.totals,
        "triggered_by": execution.triggered_by,
        "created_at": execution.created_at,
        "started_at": execution.started_at,
        "finished_at": execution.finished_at,
    }


def _execution(view: ExecutionView) -> ExecutionResponse:
    results = []
    for result in view.results:
        case = view.cases.get(result.test_case_id)
        version = view.versions.get(result.test_case_version_id)
        results.append(
            ResultResponse(
                id=result.id,
                test_case_id=result.test_case_id,
                case_key=case.key if case else "",
                title=version.title if version else "",
                version=version.version if version else 0,
                status=result.status,
                attempt_count=result.attempt_count,
                failure_class=result.failure_class,
                failure_category=result.failure_category,
                failure_summary=result.failure_summary,
                healed=result.healed,
                has_ai_suggestion=result.ai_analysis is not None,
                browser=result.browser,
                viewport=result.viewport,
                started_at=result.started_at,
                finished_at=result.finished_at,
                duration_ms=result.duration_ms,
                details=result.details,
            )
        )
    execution = view.execution
    return ExecutionResponse.model_validate(
        {
            **_summary(execution),
            "config": execution.config,
            "error": execution.error,
            "results": results,
        }
    )


def _evidence(item: TestEvidence) -> EvidenceResponse:
    return EvidenceResponse(
        id=item.id,
        result_id=item.result_id,
        step_id=item.execution_step_id,
        kind=item.kind,
        content_type=item.content_type,
        size_bytes=item.size_bytes,
        sha256=item.sha256,
        captured_at=item.captured_at,
        expires_at=item.expires_at,
        redaction_status=item.redaction_status,
    )


@router.post(
    "/executions",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Run an approved suite or approved cases against an environment",
    responses={**_WRITE, 429: _ERR, 503: _ERR},
)
async def start_execution(
    body: ExecutionCreate, ctx: ProjectContextDep, service: Executions
) -> ExecutionResponse:
    """Runs in the background with the deterministic engine. Poll the execution for results."""
    return _execution(await service.start(ctx, body))


@router.get("/executions", summary="List executions, newest first", responses=_READ)
async def list_executions(ctx: ProjectContextDep, service: Executions) -> list[ExecutionSummary]:
    return [ExecutionSummary.model_validate(_summary(e)) for e in await service.list_recent(ctx)]


@router.get("/executions/{execution_id}", summary="An execution with its results", responses=_READ)
async def get_execution(
    execution_id: uuid.UUID, ctx: ProjectContextDep, service: Executions
) -> ExecutionResponse:
    return _execution(await service.view(ctx, await service.get(ctx, execution_id)))


@router.post("/executions/{execution_id}/cancel", summary="Stop an execution", responses=_WRITE)
async def cancel_execution(
    execution_id: uuid.UUID, ctx: ProjectContextDep, service: Executions
) -> ExecutionSummary:
    return ExecutionSummary.model_validate(_summary(await service.cancel(ctx, execution_id)))


@router.get(
    "/executions/{execution_id}/results/{result_id}/steps",
    summary="Every step of one result, for every attempt",
    responses=_READ,
)
async def list_steps(
    execution_id: uuid.UUID, result_id: uuid.UUID, ctx: ProjectContextDep, service: Executions
) -> list[StepResponse]:
    steps, evidence = await service.steps(ctx, await service.get(ctx, execution_id), result_id)
    return [
        StepResponse(
            id=step.id,
            attempt=step.attempt,
            position=step.position,
            phase=step.phase,
            action=step.action,
            description=step.description,
            status=step.status,
            started_at=step.started_at,
            finished_at=step.finished_at,
            duration_ms=step.duration_ms,
            script_index=step.script_index,
            locator=step.resolved_locator,
            healed_from=step.healed_from,
            expected=step.expected,
            actual=step.actual,
            error_type=step.error_type,
            error_message=step.error_message,
            page_url=step.page_url,
            evidence_ids=evidence.get(step.id, []),
        )
        for step in steps
    ]


@router.get(
    "/executions/{execution_id}/evidence", summary="Evidence files of an execution", responses=_READ
)
async def list_evidence(
    execution_id: uuid.UUID, ctx: ProjectContextDep, service: Executions
) -> list[EvidenceResponse]:
    return [
        _evidence(item)
        for item in await service.evidence(ctx, await service.get(ctx, execution_id))
    ]
