"""CI: API keys, starting an execution from a pipeline, and reading its verified outcome."""

import re
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.deps import (
    AuditServiceDep,
    DbSessionDep,
    OrgContextDep,
    RedisDep,
    SettingsDep,
    get_execution_service,
)
from app.core.errors import NotFoundError, RateLimitedError, ValidationFailedError
from app.core.rate_limit import RateLimiter
from app.models import ApiKey, TestExecution
from app.reporting.builder import build_execution_report
from app.reporting.ci import ci_outcome, to_junit
from app.reporting.model import ExecutionReport
from app.repositories.executions import ExecutionRepository, ResultRepository
from app.repositories.projects import ProjectRepository
from app.repositories.test_cases import TestCaseRepository
from app.schemas.ci import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyResponse,
    CiExecutionCreate,
    CiExecutionResponse,
    CiFailure,
)
from app.schemas.common import ProblemDetail
from app.schemas.executions import ExecutionCreate
from app.services.api_keys import (
    SCOPE_READ,
    SCOPE_RUN,
    ApiKeyContext,
    ApiKeyService,
    authenticate,
)
from app.services.executions import Automation, ExecutionService

router = APIRouter(tags=["ci"])
_bearer = HTTPBearer(auto_error=False, scheme_name="ApiKey", description="An API key (`qak_…`).")
_ERR: dict[str, Any] = {"model": ProblemDetail}
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9._:/@-]{8,200}$")
Executions = Annotated[ExecutionService, Depends(get_execution_service)]


def get_api_key_service(session: DbSessionDep, audit: AuditServiceDep) -> ApiKeyService:
    return ApiKeyService(session=session, audit=audit)


Keys = Annotated[ApiKeyService, Depends(get_api_key_service)]


async def get_api_key(
    session: DbSessionDep,
    redis: RedisDep,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> ApiKeyContext:
    context = await authenticate(session, credentials.credentials if credentials else None)
    used = await RateLimiter(redis).hit(
        "ci", str(context.key.id), limit=settings.ci_rate_limit, window_s=60
    )
    if used.count > used.limit:
        raise RateLimitedError(
            "This API key is making too many requests.",
            headers={"Retry-After": str(used.retry_after_s)},
        )
    return context


KeyDep = Annotated[ApiKeyContext, Depends(get_api_key)]


def _key(key: ApiKey) -> dict[str, Any]:
    return {
        "id": key.id,
        "name": key.name,
        "key_prefix": key.key_prefix,
        "scopes": list(key.scopes),
        "project_id": key.project_id,
        "created_at": key.created_at,
        "last_used_at": key.last_used_at,
        "expires_at": key.expires_at,
        "revoked_at": key.revoked_at,
    }


# --- keys (managed by organization admins, with their own session) ------------------------


@router.get(
    "/organizations/{organization_id}/api-keys",
    summary="API keys of the organization. The keys themselves are never shown again",
    responses={401: _ERR, 403: _ERR, 404: _ERR},
)
async def list_api_keys(org: OrgContextDep, keys: Keys) -> list[ApiKeyResponse]:
    return [ApiKeyResponse.model_validate(_key(key)) for key in await keys.list(org)]


@router.post(
    "/organizations/{organization_id}/api-keys",
    status_code=status.HTTP_201_CREATED,
    summary="Create an API key. The answer is the only time the key is shown",
    responses={401: _ERR, 403: _ERR, 404: _ERR, 422: _ERR},
)
async def create_api_key(org: OrgContextDep, keys: Keys, data: ApiKeyCreate) -> ApiKeyCreated:
    key, secret = await keys.create(
        org,
        name=data.name,
        scopes=data.scopes,
        project_id=data.project_id,
        expires_in_days=data.expires_in_days,
    )
    return ApiKeyCreated.model_validate({**_key(key), "key": secret})


@router.delete(
    "/organizations/{organization_id}/api-keys/{key_id}",
    summary="Revoke an API key. It stops working at once",
    responses={401: _ERR, 403: _ERR, 404: _ERR},
)
async def revoke_api_key(key_id: uuid.UUID, org: OrgContextDep, keys: Keys) -> ApiKeyResponse:
    return ApiKeyResponse.model_validate(_key(await keys.revoke(org, key_id)))


# --- the CI interface (authenticated with an API key) -------------------------------------


async def _execution(
    session: DbSessionDep, key: ApiKeyContext, execution_id: uuid.UUID
) -> TestExecution:
    execution = await ExecutionRepository(session, key.organization_id).get(execution_id)
    if execution is None:
        raise NotFoundError("Execution not found.")
    key.require_project(execution.project_id)
    return execution


async def _response(
    session: DbSessionDep, settings: SettingsDep, execution: TestExecution
) -> CiExecutionResponse:
    organization_id = execution.organization_id
    results = await ResultRepository(session, organization_id).for_execution(execution.id)
    cases = await TestCaseRepository(session, organization_id).by_ids_any(
        [r.test_case_id for r in results]
    )
    outcome = ci_outcome(execution.status, execution.verdict, execution.totals)
    failures = [
        CiFailure(
            case_key=cases[r.test_case_id].key if r.test_case_id in cases else str(r.test_case_id),
            title=cases[r.test_case_id].title if r.test_case_id in cases else "",
            status=r.status,
            failure_category=r.failure_category,
            summary=r.failure_summary,
        )
        for r in results
        if r.status in ("failed", "error", "flaky", "needs_review")
    ]
    return CiExecutionResponse(
        id=execution.id,
        project_id=execution.project_id,
        status=execution.status,
        verdict=execution.verdict,
        **outcome.as_dict(),
        totals=execution.totals,
        failures=failures[:50],
        report_url=(
            f"{settings.public_base_url.rstrip('/')}/projects/{execution.project_id}"
            f"/executions/{execution.id}"
        ),
        created_at=execution.created_at,
        started_at=execution.started_at,
        finished_at=execution.finished_at,
    )


@router.post(
    "/ci/executions",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start an execution of approved tests from a pipeline",
    responses={
        200: {"model": CiExecutionResponse},
        401: _ERR,
        403: _ERR,
        404: _ERR,
        409: _ERR,
        422: _ERR,
        429: _ERR,
    },
)
async def start_ci_execution(
    data: CiExecutionCreate,
    response: Response,
    key: KeyDep,
    session: DbSessionDep,
    settings: SettingsDep,
    executions: Executions,
    idempotency_key: Annotated[
        str | None,
        Header(description="Repeat the request with the same value to get the same execution."),
    ] = None,
) -> CiExecutionResponse:
    key.require(SCOPE_RUN)
    key.require_project(data.project_id)
    if idempotency_key is not None and not _IDEMPOTENCY.match(idempotency_key):
        raise ValidationFailedError("The Idempotency-Key header is not valid.")
    project = await ProjectRepository(session, key.organization_id).get(data.project_id)
    if project is None:
        raise NotFoundError("Project not found.")
    view, created = await executions.start_automated(
        organization_id=key.organization_id,
        project=project,
        data=ExecutionCreate(
            environment_id=data.environment_id,
            suite_id=data.suite_id,
            case_ids=data.case_ids,
            config=data.config,
        ),
        automation=Automation(
            trigger="ci",
            actor_type="api_key",
            actor_id=key.key.id,
            api_key_id=key.key.id,
            git_context=(
                {"source": "ci", **data.git.model_dump(exclude_none=True)} if data.git else None
            ),
            idempotency_key=(
                f"ci:{project.id}:{idempotency_key}" if idempotency_key is not None else None
            ),
        ),
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return await _response(session, settings, view.execution)


@router.get(
    "/ci/executions/{execution_id}",
    summary="Status and verified outcome of an execution, for a pipeline to poll",
    responses={401: _ERR, 403: _ERR, 404: _ERR, 429: _ERR},
)
async def ci_execution_status(
    execution_id: uuid.UUID, key: KeyDep, session: DbSessionDep, settings: SettingsDep
) -> CiExecutionResponse:
    key.require(SCOPE_READ)
    return await _response(session, settings, await _execution(session, key, execution_id))


async def _report(
    session: DbSessionDep, key: ApiKeyContext, execution_id: uuid.UUID
) -> ExecutionReport:
    key.require(SCOPE_READ)
    execution = await _execution(session, key, execution_id)
    project = await ProjectRepository(session, key.organization_id).get(execution.project_id)
    if project is None:
        raise NotFoundError("Execution not found.")
    return await build_execution_report(session, key.organization_id, project, execution)


@router.get(
    "/ci/executions/{execution_id}/report",
    summary="The full test report as JSON",
    responses={401: _ERR, 403: _ERR, 404: _ERR, 429: _ERR},
)
async def ci_execution_report(
    execution_id: uuid.UUID, key: KeyDep, session: DbSessionDep
) -> ExecutionReport:
    return await _report(session, key, execution_id)


@router.get(
    "/ci/executions/{execution_id}/junit",
    summary="The results as JUnit XML",
    responses={401: _ERR, 403: _ERR, 404: _ERR, 429: _ERR},
    response_class=Response,
)
async def ci_execution_junit(
    execution_id: uuid.UUID, key: KeyDep, session: DbSessionDep
) -> Response:
    report = await _report(session, key, execution_id)
    return Response(
        to_junit(report),
        media_type="application/xml",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
    )
