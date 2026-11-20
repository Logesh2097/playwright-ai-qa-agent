"""GitHub: connections, linked repositories, their settings, and the webhook endpoint."""

import uuid
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Path, Request, Response, status

from app.api.deps import (
    AuditServiceDep,
    DbSessionDep,
    OrgContextDep,
    ProjectContextDep,
    RedisDep,
    SettingsDep,
)
from app.core.errors import RateLimitedError, ValidationFailedError
from app.core.rate_limit import RateLimiter
from app.models import GithubDelivery, GithubRepository, Integration
from app.schemas.common import ProblemDetail
from app.schemas.github import (
    AvailableRepository,
    CommitResponse,
    ConnectionCreate,
    ConnectionResponse,
    DeliveryResponse,
    ImpactRequest,
    LinkCreate,
    LinkResponse,
    LinkSecretResponse,
    LinkUpdate,
    MetadataResponse,
    PullRequestResponse,
    SelectionResponse,
    WebhookAnswer,
)
from app.services.github import GithubClients, GithubService

router = APIRouter(tags=["github"])
_ERR: dict[str, Any] = {"model": ProblemDetail}
_READ: dict[int | str, dict[str, Any]] = {401: _ERR, 403: _ERR, 404: _ERR, 503: _ERR}
_WRITE: dict[int | str, dict[str, Any]] = {**_READ, 409: _ERR, 422: _ERR}
_ORG = "/organizations/{organization_id}"
_PROJECT = _ORG + "/projects/{project_id}"
# Deliveries one linked repository may send per minute.
WEBHOOK_LIMIT = 300


def get_github_service(
    request: Request,
    session: DbSessionDep,
    audit: AuditServiceDep,
    redis: RedisDep,
    settings: SettingsDep,
) -> GithubService:
    return GithubService(
        session=session,
        audit=audit,
        jobs=request.app.state.job_queue,
        redis=redis,
        settings=settings,
        clients=GithubClients(
            settings,
            request.app.state.credential_cipher,
            getattr(request.app.state, "github_transport", None),
        ),
    )


Github = Annotated[GithubService, Depends(get_github_service)]


def _connection(row: Integration) -> ConnectionResponse:
    return ConnectionResponse(
        id=row.id,
        name=str(row.config.get("name", "")),
        auth="app" if row.config.get("auth") == "app" else "token",
        account=row.external_account,
        status=row.status,
        created_at=row.created_at,
    )


def _link(service: GithubService, row: GithubRepository) -> dict[str, Any]:
    return {
        "id": row.id,
        "connection_id": row.integration_id,
        "github_repo_id": row.github_repo_id,
        "full_name": row.full_name,
        "default_branch": row.default_branch,
        "private": row.is_private,
        "enabled": row.enabled,
        "on_pull_request": row.on_pull_request,
        "on_push": row.on_push,
        "branches": list(row.branches),
        "environment_id": row.environment_id,
        "suite_ids": list(row.suite_ids),
        "selection_mode": row.selection_mode,
        "path_rules": list(row.path_rules),
        "status_enabled": row.status_enabled,
        "comment_enabled": row.comment_enabled,
        "webhook_url": service.webhook_path(row),
        "created_at": row.created_at,
    }


def _delivery(row: GithubDelivery) -> DeliveryResponse:
    return DeliveryResponse(
        id=row.id,
        delivery_id=row.delivery_id,
        event=row.event,
        action=row.action,
        outcome=row.outcome,
        reason=row.reason,
        subject=row.subject,
        execution_ids=list(row.execution_ids),
        received_at=row.created_at,
    )


# --- connections --------------------------------------------------------------------------


@router.get(_ORG + "/github/connections", summary="GitHub connections", responses=_READ)
async def list_connections(org: OrgContextDep, github: Github) -> list[ConnectionResponse]:
    return [_connection(row) for row in await github.connections(org)]


@router.post(
    _ORG + "/github/connections",
    status_code=status.HTTP_201_CREATED,
    summary="Connect to GitHub with a fine-grained token or a GitHub App installation",
    responses=_WRITE,
)
async def create_connection(
    org: OrgContextDep, github: Github, data: ConnectionCreate
) -> ConnectionResponse:
    row = await github.connect(
        org,
        name=data.name,
        token=data.token.get_secret_value() if data.token else None,
        app_id=data.app_id,
        private_key=data.private_key.get_secret_value() if data.private_key else None,
        installation_id=data.installation_id,
    )
    return _connection(row)


@router.delete(
    _ORG + "/github/connections/{connection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a connection, its credentials and its repository links",
    responses=_READ,
)
async def delete_connection(
    connection_id: uuid.UUID, org: OrgContextDep, github: Github
) -> Response:
    await github.disconnect(org, connection_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    _ORG + "/github/connections/{connection_id}/repositories",
    summary="Repositories the connection's credentials can reach",
    responses=_READ,
)
async def list_available_repositories(
    connection_id: uuid.UUID, org: OrgContextDep, github: Github
) -> list[AvailableRepository]:
    found = await github.available_repositories(org, connection_id)
    return [AvailableRepository.model_validate(asdict(item)) for item in found]


# --- linked repositories ------------------------------------------------------------------


@router.get(
    _PROJECT + "/github/repositories", summary="Repositories linked to the project", responses=_READ
)
async def list_links(ctx: ProjectContextDep, github: Github) -> list[LinkResponse]:
    return [LinkResponse.model_validate(_link(github, row)) for row in await github.links(ctx)]


@router.post(
    _PROJECT + "/github/repositories",
    status_code=status.HTTP_201_CREATED,
    summary="Link a repository. The answer holds the webhook secret, shown once",
    responses=_WRITE,
)
async def create_link(
    ctx: ProjectContextDep, github: Github, data: LinkCreate
) -> LinkSecretResponse:
    created = await github.link(ctx, connection_id=data.connection_id, full_name=data.full_name)
    return LinkSecretResponse.model_validate(
        {**_link(github, created.repository), "webhook_secret": created.webhook_secret}
    )


@router.get(
    _PROJECT + "/github/repositories/{link_id}", summary="One linked repository", responses=_READ
)
async def get_link(link_id: uuid.UUID, ctx: ProjectContextDep, github: Github) -> LinkResponse:
    return LinkResponse.model_validate(_link(github, await github.link_of(ctx, link_id)))


@router.patch(
    _PROJECT + "/github/repositories/{link_id}",
    summary="Change what a linked repository may trigger and whether results are written back",
    responses=_WRITE,
)
async def update_link(
    link_id: uuid.UUID, ctx: ProjectContextDep, github: Github, data: LinkUpdate
) -> LinkResponse:
    row = await github.configure(ctx, link_id, data.model_dump(exclude_unset=True))
    return LinkResponse.model_validate(_link(github, row))


@router.delete(
    _PROJECT + "/github/repositories/{link_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unlink a repository",
    responses=_READ,
)
async def delete_link(link_id: uuid.UUID, ctx: ProjectContextDep, github: Github) -> Response:
    await github.unlink(ctx, link_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    _PROJECT + "/github/repositories/{link_id}/webhook-secret",
    summary="Replace the webhook secret. The new one is shown once",
    responses=_WRITE,
)
async def rotate_webhook_secret(
    link_id: uuid.UUID, ctx: ProjectContextDep, github: Github
) -> LinkSecretResponse:
    created = await github.rotate_secret(ctx, link_id)
    return LinkSecretResponse.model_validate(
        {**_link(github, created.repository), "webhook_secret": created.webhook_secret}
    )


@router.get(
    _PROJECT + "/github/repositories/{link_id}/metadata",
    summary="The repository and its branches, as GitHub has them now",
    responses=_READ,
)
async def repository_metadata(
    link_id: uuid.UUID, ctx: ProjectContextDep, github: Github
) -> MetadataResponse:
    found = await github.metadata(ctx, link_id)
    return MetadataResponse.model_validate(
        {"repository": asdict(found["repository"]), "branches": found["branches"]}
    )


@router.get(
    _PROJECT + "/github/repositories/{link_id}/pull-requests/{number}",
    summary="A pull request, its changed files, and the suites the change concerns",
    responses=_READ,
)
async def pull_request_info(
    link_id: uuid.UUID,
    number: Annotated[int, Path(ge=1)],
    ctx: ProjectContextDep,
    github: Github,
) -> PullRequestResponse:
    found = await github.pull_request(ctx, link_id, number)
    pull, files = found["pull_request"], found["files"]
    return PullRequestResponse(
        number=pull.number,
        title=pull.title,
        state=pull.state,
        draft=pull.draft,
        author=pull.author,
        html_url=pull.html_url,
        head_sha=pull.head_sha,
        head_ref=pull.head_ref,
        base_ref=pull.base_ref,
        from_fork=pull.from_fork,
        changed_files=files.paths[:3000],
        changed_files_complete=files.complete,
        selection=SelectionResponse.model_validate(found["selection"].as_dict()),
    )


@router.get(
    _PROJECT + "/github/repositories/{link_id}/commits/{sha}",
    summary="A commit of the linked repository",
    responses=_READ,
)
async def commit_info(
    link_id: uuid.UUID,
    sha: Annotated[str, Path(pattern=r"^[0-9a-f]{7,64}$")],
    ctx: ProjectContextDep,
    github: Github,
) -> CommitResponse:
    return CommitResponse.model_validate(asdict(await github.commit(ctx, link_id, sha)))


@router.post(
    _PROJECT + "/github/repositories/{link_id}/impact",
    summary="Which suites a list of changed files concerns, by the path rules",
    responses=_WRITE,
)
async def impact(
    link_id: uuid.UUID, ctx: ProjectContextDep, github: Github, data: ImpactRequest
) -> SelectionResponse:
    row = await github.link_of(ctx, link_id)
    return SelectionResponse.model_validate(github.suggest(row, data.files, True).as_dict())


@router.get(
    _PROJECT + "/github/repositories/{link_id}/deliveries",
    summary="Recent webhook deliveries and what was done with each",
    responses=_READ,
)
async def list_deliveries(
    link_id: uuid.UUID, ctx: ProjectContextDep, github: Github
) -> list[DeliveryResponse]:
    return [_delivery(row) for row in await github.deliveries(ctx, link_id)]


# --- the webhook (called by GitHub; authenticated by its signature) ------------------------


@router.post(
    "/webhooks/github/{organization_id}/{hook_token}",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Receives GitHub webhook deliveries for one linked repository",
    responses={200: {"model": WebhookAnswer}, 400: _ERR, 401: _ERR, 413: _ERR, 429: _ERR},
)
async def github_webhook(
    organization_id: uuid.UUID,
    hook_token: Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{20,64}$")],
    request: Request,
    response: Response,
    github: Github,
    redis: RedisDep,
    settings: SettingsDep,
    x_hub_signature_256: Annotated[str | None, Header()] = None,
    x_github_delivery: Annotated[str | None, Header()] = None,
    x_github_event: Annotated[str | None, Header()] = None,
) -> WebhookAnswer:
    used = await RateLimiter(redis).hit(
        "github_webhook", hook_token, limit=WEBHOOK_LIMIT, window_s=60
    )
    if used.count > used.limit:
        raise RateLimitedError(headers={"Retry-After": str(used.retry_after_s)})
    limit = settings.github_webhook_max_bytes
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise ValidationFailedError("The payload is too large.", code="payload_too_large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise ValidationFailedError("The payload is too large.", code="payload_too_large")
    received = await github.receive(
        organization_id=organization_id,
        hook_token=hook_token,
        signature=x_hub_signature_256,
        delivery_id=x_github_delivery,
        event=x_github_event,
        body=bytes(body),
    )
    if received.status != "accepted":
        response.status_code = status.HTTP_200_OK
    return WebhookAnswer(status=received.status, reason=received.reason)
