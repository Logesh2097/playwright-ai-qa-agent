"""Test cases and suites: generate, review, edit, approve."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import (
    InstructionServiceDep,
    ProjectContextDep,
    SuiteServiceDep,
    TestCaseServiceDep,
)
from app.api.v1.instructions import InstructionDep
from app.api.v1.instructions import _response as instruction_response
from app.models import TestCase, TestCaseVersion, TestSuite
from app.schemas.common import ProblemDetail
from app.schemas.instructions import InstructionResponse
from app.schemas.test_cases import (
    CaseCreate,
    CaseResponse,
    CaseStatus,
    CaseSummary,
    CaseValidation,
    CaseVersionResponse,
    CaseVersionSummary,
    RegenerateRequest,
    SuiteCaseResponse,
    SuiteCases,
    SuiteCreate,
    SuiteResponse,
    SuiteStatus,
    SuiteUpdate,
)
from app.services.test_cases import CaseView
from app.services.test_suites import SuiteView
from app.testing.cases import CaseContent, Category

BASE = "/organizations/{organization_id}/projects/{project_id}"
router = APIRouter(prefix=BASE, tags=["test cases"])

Responses = dict[int | str, dict[str, Any]]
_ERR: dict[str, Any] = {"model": ProblemDetail}
_READ: Responses = {401: _ERR, 404: _ERR}
_WRITE: Responses = {401: _ERR, 403: _ERR, 404: _ERR, 409: _ERR, 422: _ERR}
_AI: Responses = {**_WRITE, 429: _ERR, 503: _ERR}


async def get_case(
    case_id: uuid.UUID, ctx: ProjectContextDep, service: TestCaseServiceDep
) -> TestCase:
    return await service.get(ctx, case_id)


async def get_suite(
    suite_id: uuid.UUID, ctx: ProjectContextDep, service: SuiteServiceDep
) -> TestSuite:
    return await service.get(ctx, suite_id)


CaseDep = Annotated[TestCase, Depends(get_case)]
SuiteDep = Annotated[TestSuite, Depends(get_suite)]


def _validation(version: TestCaseVersion) -> CaseValidation:
    return CaseValidation.model_validate(version.validation or {"ready": False, "issues": []})


def _summary(view: CaseView) -> dict[str, Any]:
    validation = _validation(view.version)
    case = view.case
    return {
        "id": case.id,
        "key": case.key,
        "title": view.content.title,
        "category": view.content.category,
        "test_type": view.content.test_type,
        "priority": view.content.priority,
        "severity": view.content.severity,
        "tags": view.content.tags,
        "status": case.status,
        "source": case.source,
        "instruction_id": case.instruction_id,
        "objective_id": view.content.objective_id,
        "version": view.version.version,
        "version_source": view.version.source,
        "has_expected_result": view.content.expected_result is not None,
        "ready": validation.ready,
        "blocking_issues": sum(1 for i in validation.issues if i.severity == "blocking"),
        "updated_at": case.updated_at,
    }


def _case(view: CaseView, numbers: dict[uuid.UUID, int]) -> CaseResponse:
    case = view.case
    return CaseResponse.model_validate(
        {
            **_summary(view),
            "project_id": case.project_id,
            "application_id": case.application_id,
            "environment_id": case.environment_id,
            "content": view.content,
            "validation": _validation(view.version),
            "original_version": numbers.get(case.original_version_id)
            if case.original_version_id
            else None,
            "approved_version": numbers.get(case.approved_version_id)
            if case.approved_version_id
            else None,
            "created_at": case.created_at,
        }
    )


def _version(version: TestCaseVersion, case: TestCase) -> dict[str, Any]:
    return {
        "version": version.version,
        "source": version.source,
        "change_reason": version.change_reason,
        "title": version.title,
        "created_by": version.created_by,
        "created_at": version.created_at,
        "approved_by": version.approved_by,
        "approved_at": version.approved_at,
        "is_current": version.id == case.current_version_id,
        "is_original": version.id == case.original_version_id,
        "is_approved": version.id == case.approved_version_id,
    }


def _suite(view: SuiteView) -> SuiteResponse:
    suite = view.suite
    return SuiteResponse(
        id=suite.id,
        project_id=suite.project_id,
        application_id=suite.application_id,
        instruction_id=suite.instruction_id,
        name=suite.name,
        description=suite.description,
        feature=suite.feature,
        priority=suite.priority,
        tags=list(suite.tags),
        status=suite.status,
        source=suite.source,
        approved_by=suite.approved_by,
        approved_at=suite.approved_at,
        cases=[
            SuiteCaseResponse(
                position=member.position,
                id=member.case.id,
                key=member.case.key,
                title=member.case.title,
                category=member.case.category,
                priority=member.case.priority,
                status=member.case.status,
                current_version=member.current_version,
                approved_version=member.approved_version,
                outdated=member.outdated,
            )
            for member in view.members
        ],
        created_at=suite.created_at,
        updated_at=suite.updated_at,
    )


# --- generation --------------------------------------------------------------------------


@router.post(
    "/instructions/{instruction_id}/generate",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Generate test cases from the approved requirement",
    tags=["instructions"],
    responses=_AI,
)
async def generate_cases(
    ctx: ProjectContextDep,
    instruction: InstructionDep,
    cases: TestCaseServiceDep,
    instructions: InstructionServiceDep,
) -> InstructionResponse:
    """Runs in the background: the instruction's status becomes `generating`, then
    `awaiting_approval`. `generation` on the instruction reports what was produced."""
    instruction = await cases.generate(ctx, instruction)
    return instruction_response(instruction, await instructions.usage(ctx, instruction))


@router.post(
    "/instructions/{instruction_id}/regenerate",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Have selected cases rewritten",
    tags=["instructions"],
    responses=_AI,
)
async def regenerate_cases(
    data: RegenerateRequest,
    ctx: ProjectContextDep,
    instruction: InstructionDep,
    cases: TestCaseServiceDep,
    instructions: InstructionServiceDep,
) -> InstructionResponse:
    """Each selected case gets a new version; its earlier versions are kept."""
    instruction = await cases.regenerate(
        ctx, instruction, case_ids=data.case_ids, feedback=data.feedback
    )
    return instruction_response(instruction, await instructions.usage(ctx, instruction))


# --- test cases --------------------------------------------------------------------------


@router.get("/test-cases", summary="List test cases", responses=_READ)
async def list_cases(
    ctx: ProjectContextDep,
    service: TestCaseServiceDep,
    instruction_id: uuid.UUID | None = None,
    status_filter: Annotated[CaseStatus | None, Query(alias="status")] = None,
    category: Category | None = None,
) -> list[CaseSummary]:
    views = await service.list_views(
        ctx, instruction_id=instruction_id, status=status_filter, category=category
    )
    return [CaseSummary.model_validate(_summary(view)) for view in views]


@router.post(
    "/test-cases",
    status_code=status.HTTP_201_CREATED,
    summary="Add a test case by hand",
    responses=_WRITE,
)
async def create_case(
    data: CaseCreate, ctx: ProjectContextDep, service: TestCaseServiceDep
) -> CaseResponse:
    case = await service.create_manual(
        ctx,
        application_id=data.application_id,
        environment_id=data.environment_id,
        instruction_id=data.instruction_id,
        content=data.content,
    )
    return _case(await service.view(ctx, case), await service.version_numbers(ctx, case))


@router.get("/test-cases/{case_id}", summary="Get a test case", responses=_READ)
async def get_case_route(
    ctx: ProjectContextDep, case: CaseDep, service: TestCaseServiceDep
) -> CaseResponse:
    """`content` is the current version. `validation` lists what blocks approval."""
    return _case(await service.view(ctx, case), await service.version_numbers(ctx, case))


@router.put("/test-cases/{case_id}", summary="Edit a test case", responses=_WRITE)
async def edit_case(
    content: CaseContent, ctx: ProjectContextDep, case: CaseDep, service: TestCaseServiceDep
) -> CaseResponse:
    """Saved as a new version. Earlier versions, including the AI's original, are kept.
    An approved case needs review again."""
    case = await service.edit(ctx, case, content)
    return _case(await service.view(ctx, case), await service.version_numbers(ctx, case))


@router.delete(
    "/test-cases/{case_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a test case",
    responses=_WRITE,
)
async def delete_case(ctx: ProjectContextDep, case: CaseDep, service: TestCaseServiceDep) -> None:
    await service.delete(ctx, case)


@router.post(
    "/test-cases/{case_id}/approve",
    summary="Approve a test case (QA engineer, admin, owner)",
    responses=_WRITE,
)
async def approve_case_route(
    ctx: ProjectContextDep, case: CaseDep, service: TestCaseServiceDep
) -> CaseResponse:
    """Approves the current version. Refused with `case_not_ready` while an issue blocks."""
    case = await service.approve(ctx, case)
    return _case(await service.view(ctx, case), await service.version_numbers(ctx, case))


@router.get("/test-cases/{case_id}/versions", summary="Version history", responses=_READ)
async def list_versions(
    ctx: ProjectContextDep, case: CaseDep, service: TestCaseServiceDep
) -> list[CaseVersionSummary]:
    return [
        CaseVersionSummary.model_validate(_version(v, case))
        for v in await service.versions(ctx, case)
    ]


@router.get(
    "/test-cases/{case_id}/versions/{number}", summary="One version in full", responses=_READ
)
async def get_version(
    number: int, ctx: ProjectContextDep, case: CaseDep, service: TestCaseServiceDep
) -> CaseVersionResponse:
    version, content = await service.version(ctx, case, number)
    return CaseVersionResponse.model_validate(
        {
            **_version(version, case),
            "content": content,
            "validation": version.validation,
        }
    )


# --- suites ------------------------------------------------------------------------------


@router.get(
    "/test-suites", summary="List suites, grouped by feature", tags=["test suites"], responses=_READ
)
async def list_suites(
    ctx: ProjectContextDep,
    service: SuiteServiceDep,
    feature: str | None = None,
    status_filter: Annotated[SuiteStatus | None, Query(alias="status")] = None,
) -> list[SuiteResponse]:
    return [_suite(view) for view in await service.list(ctx, feature=feature, status=status_filter)]


@router.post(
    "/test-suites",
    status_code=status.HTTP_201_CREATED,
    summary="Create a suite",
    tags=["test suites"],
    responses=_WRITE,
)
async def create_suite(
    data: SuiteCreate, ctx: ProjectContextDep, service: SuiteServiceDep
) -> SuiteResponse:
    return _suite(await service.view(ctx, await service.create(ctx, data)))


@router.get("/test-suites/{suite_id}", summary="Get a suite", tags=["test suites"], responses=_READ)
async def get_suite_route(
    ctx: ProjectContextDep, suite: SuiteDep, service: SuiteServiceDep
) -> SuiteResponse:
    return _suite(await service.view(ctx, suite))


@router.patch(
    "/test-suites/{suite_id}", summary="Update a suite", tags=["test suites"], responses=_WRITE
)
async def update_suite(
    data: SuiteUpdate, ctx: ProjectContextDep, suite: SuiteDep, service: SuiteServiceDep
) -> SuiteResponse:
    return _suite(await service.view(ctx, await service.update(ctx, suite, data)))


@router.delete(
    "/test-suites/{suite_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a suite (its cases are kept)",
    tags=["test suites"],
    responses=_WRITE,
)
async def delete_suite(ctx: ProjectContextDep, suite: SuiteDep, service: SuiteServiceDep) -> None:
    await service.delete(ctx, suite)


@router.put(
    "/test-suites/{suite_id}/cases",
    summary="Set the suite's cases and their execution order",
    tags=["test suites"],
    responses=_WRITE,
)
async def set_suite_cases(
    data: SuiteCases, ctx: ProjectContextDep, suite: SuiteDep, service: SuiteServiceDep
) -> SuiteResponse:
    """Send the complete list in the order the cases should run. Adding, removing or
    reordering turns an approved suite back into a draft."""
    return _suite(await service.view(ctx, await service.set_cases(ctx, suite, data.case_ids)))


@router.post(
    "/test-suites/{suite_id}/approve",
    summary="Approve a suite and its cases (QA engineer, admin, owner)",
    tags=["test suites"],
    responses=_WRITE,
)
async def approve_suite(
    ctx: ProjectContextDep, suite: SuiteDep, service: SuiteServiceDep
) -> SuiteResponse:
    """All or nothing. Every case is checked again; if one is not ready, nothing is
    approved and `errors` lists what blocks. On success the approved version of each
    case is recorded with the suite."""
    return _suite(await service.view(ctx, await service.approve(ctx, suite)))
