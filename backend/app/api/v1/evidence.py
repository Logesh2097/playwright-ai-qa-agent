"""Evidence files: reading one with a session, and short-lived download addresses."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.api.deps import (
    CurrentUser,
    CurrentUserDep,
    DbSessionDep,
    ProjectContextDep,
    ProjectServiceDep,
    RedisDep,
    SettingsDep,
    get_evidence_access,
    get_execution_service,
    get_org_context,
)
from app.core.errors import UnauthenticatedError
from app.db.context import set_current_user
from app.repositories.identity import UserRepository
from app.schemas.common import ProblemDetail
from app.schemas.executions import EvidenceDownloadUrl
from app.services.auth import revoked_session_key
from app.services.evidence_access import EvidenceAccess, EvidenceFile, verify_download
from app.services.executions import ExecutionService

router = APIRouter(tags=["evidence"])
Access = Annotated[EvidenceAccess, Depends(get_evidence_access)]
Executions = Annotated[ExecutionService, Depends(get_execution_service)]

_ERR: dict[str, Any] = {"model": ProblemDetail}
_READ: dict[int | str, dict[str, Any]] = {401: _ERR, 404: _ERR}
_PROJECT = "/organizations/{organization_id}/projects/{project_id}"


def _serve(file: EvidenceFile) -> StreamingResponse:
    return StreamingResponse(
        file.chunks,
        media_type=file.item.content_type,
        headers={
            # Evidence shows the application under test; it is never rendered as a page of ours.
            "Content-Disposition": f'attachment; filename="{file.filename}"',
            "Content-Length": str(file.item.size_bytes),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


@router.get(
    _PROJECT + "/executions/{execution_id}/evidence/{evidence_id}/content",
    summary="Download one evidence file",
    responses=_READ,
    response_class=StreamingResponse,
)
async def evidence_content(
    execution_id: uuid.UUID,
    evidence_id: uuid.UUID,
    ctx: ProjectContextDep,
    executions: Executions,
    access: Access,
) -> StreamingResponse:
    execution = await executions.get(ctx, execution_id)
    return _serve(await access.open(ctx, execution, evidence_id))


@router.post(
    _PROJECT + "/executions/{execution_id}/evidence/{evidence_id}/download-url",
    summary="A download address for one file, valid for a few minutes, for the caller only",
    responses=_READ,
)
async def evidence_download_url(
    execution_id: uuid.UUID,
    evidence_id: uuid.UUID,
    request: Request,
    ctx: ProjectContextDep,
    current: CurrentUserDep,
    executions: Executions,
    access: Access,
) -> EvidenceDownloadUrl:
    execution = await executions.get(ctx, execution_id)
    token, expires = await access.download_url(
        ctx, execution, evidence_id, session_id=current.session_id
    )
    return EvidenceDownloadUrl(
        url=request.app.url_path_for("redeem_evidence_download", token=token), expires_at=expires
    )


@router.get(
    "/evidence-downloads/{token}",
    name="redeem_evidence_download",
    summary="Download the file a download address was issued for",
    responses=_READ,
    response_class=StreamingResponse,
)
async def redeem_evidence_download(
    token: str,
    session: DbSessionDep,
    redis: RedisDep,
    settings: SettingsDep,
    projects: ProjectServiceDep,
    executions: Executions,
    access: Access,
) -> StreamingResponse:
    """The token says who asked and for which file. Everything else is checked again now."""
    claims = verify_download(
        token, secret=settings.jwt_secret_key.get_secret_value(), issuer=settings.app_name
    )
    refused = UnauthenticatedError(
        "The download address is invalid or has expired.", code="invalid_download_token"
    )
    if await redis.exists(revoked_session_key(claims.session_id)):
        raise refused
    user = await UserRepository(session).get(claims.user_id)
    if user is None or not user.is_active:
        raise refused
    await set_current_user(session, user.id)
    org = await get_org_context(
        claims.organization_id, CurrentUser(user=user, session_id=claims.session_id), session
    )
    ctx = await projects.resolve(org, claims.project_id)
    execution = await executions.get(ctx, claims.execution_id)
    return _serve(await access.open(ctx, execution, claims.evidence_id))
