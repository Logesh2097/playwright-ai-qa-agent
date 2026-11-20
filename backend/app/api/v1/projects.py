"""Projects, applications, environments and credentials.

Everything is nested under the organization and project in the path. Each level
is loaded through its parent, so an ID from another project, environment or
tenant simply is not found.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import (
    ApplicationServiceDep,
    CredentialServiceDep,
    OrgContextDep,
    ProjectContextDep,
    ProjectServiceDep,
)
from app.core.security.url_safety import Origin
from app.models import Application, Credential, Environment
from app.schemas.common import ProblemDetail
from app.schemas.projects import (
    AccessCheckHop,
    AccessCheckResponse,
    AllowedOriginResponse,
    ApplicationCreate,
    ApplicationResponse,
    ApplicationUpdate,
    CredentialCreate,
    CredentialResponse,
    CredentialUpdate,
    EnvironmentCreate,
    EnvironmentResponse,
    EnvironmentUpdate,
    ProjectCreate,
    ProjectMemberAdd,
    ProjectMemberResponse,
    ProjectMemberUpdate,
    ProjectResponse,
    ProjectUpdate,
)
from app.services.applications import EnvironmentView
from app.services.projects import ProjectView

router = APIRouter(prefix="/organizations/{organization_id}/projects", tags=["projects"])

Responses = dict[int | str, dict[str, Any]]
_ERR: dict[str, Any] = {"model": ProblemDetail}
_READ: Responses = {401: _ERR, 404: _ERR}
_WRITE: Responses = {401: _ERR, 403: _ERR, 404: _ERR, 409: _ERR, 422: _ERR}


# --- path-scoped dependencies -----------------------------------------------------------


async def get_application(
    application_id: uuid.UUID, ctx: ProjectContextDep, service: ApplicationServiceDep
) -> Application:
    return await service.get_application(ctx, application_id)


ApplicationDep = Annotated[Application, Depends(get_application)]


async def get_environment(
    environment_id: uuid.UUID,
    ctx: ProjectContextDep,
    application: ApplicationDep,
    service: ApplicationServiceDep,
) -> Environment:
    return await service.get_environment(ctx, application, environment_id)


EnvironmentDep = Annotated[Environment, Depends(get_environment)]


async def get_credential(
    credential_id: uuid.UUID,
    ctx: ProjectContextDep,
    environment: EnvironmentDep,
    service: CredentialServiceDep,
) -> Credential:
    return await service.get(ctx, environment, credential_id)


CredentialDep = Annotated[Credential, Depends(get_credential)]


# --- response builders -------------------------------------------------------------------


def _project(view: ProjectView) -> ProjectResponse:
    project = view.project
    return ProjectResponse(
        id=project.id,
        organization_id=project.organization_id,
        name=project.name,
        slug=project.slug,
        description=project.description,
        status=project.status,
        created_by=project.created_by,
        created_at=project.created_at,
        updated_at=project.updated_at,
        member_count=view.member_count,
        application_count=view.application_count,
        my_role=view.my_role,
        can_manage=view.can_manage,
    )


def _application(application: Application, environment_count: int) -> ApplicationResponse:
    return ApplicationResponse(
        id=application.id,
        project_id=application.project_id,
        name=application.name,
        description=application.description,
        base_url=application.base_url,
        type=application.type,
        framework=application.framework,
        auth_method=application.auth_method,
        default_browser_config=application.default_browser_config,
        allowed_domains=application.allowed_domains,
        status=application.status,
        environment_count=environment_count,
        created_at=application.created_at,
        updated_at=application.updated_at,
    )


def _environment(view: EnvironmentView) -> EnvironmentResponse:
    environment = view.environment
    return EnvironmentResponse.model_validate(
        {
            "id": environment.id,
            "application_id": environment.application_id,
            "name": environment.name,
            "kind": environment.kind,
            "base_url": environment.base_url,
            "api_base_url": environment.api_base_url,
            "allowed_origins": [
                AllowedOriginResponse(
                    origin=str(Origin(o.scheme, o.host, o.port)),
                    scheme=o.scheme,
                    host=o.host,
                    port=o.port,
                    purpose=o.purpose,
                )
                for o in view.origins
            ],
            "variables": environment.variables,
            "auth_config": environment.auth_config,
            "allowed_actions": environment.allowed_actions,
            "is_read_only": environment.is_read_only,
            "requires_approval": environment.requires_approval,
            "allow_private_network": environment.allow_private_network,
            "status": environment.status,
            "credential_count": view.credential_count,
            "created_at": environment.created_at,
            "updated_at": environment.updated_at,
        }
    )


def _credential(credential: Credential) -> CredentialResponse:
    """Built field by field on purpose: nothing from the encrypted columns can slip in."""
    return CredentialResponse(
        id=credential.id,
        environment_id=credential.environment_id,
        name=credential.name,
        role_label=credential.role_label,
        kind=credential.kind,
        source=credential.source,
        field_names=list(credential.field_names),
        external_ref=credential.external_ref,
        is_test_account=credential.is_test_account,
        setup_instructions=credential.setup_instructions,
        login_recipe=list(credential.login_recipe),
        last_rotated_at=credential.last_rotated_at,
        created_at=credential.created_at,
        updated_at=credential.updated_at,
    )


# --- projects ----------------------------------------------------------------------------


@router.get("", summary="Projects the caller can see", responses=_READ)
async def list_projects(ctx: OrgContextDep, service: ProjectServiceDep) -> list[ProjectResponse]:
    """Admins and owners see every project; everyone else sees the projects they belong to."""
    return [_project(view) for view in await service.list(ctx)]


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create a project", responses=_WRITE)
async def create_project(
    data: ProjectCreate, ctx: OrgContextDep, service: ProjectServiceDep
) -> ProjectResponse:
    """Developers, QA engineers, admins and owners. The creator becomes project manager."""
    project_ctx = await service.create(ctx, data)
    return _project(await service.view(project_ctx))


@router.get("/{project_id}", summary="Get a project", responses=_READ)
async def get_project(ctx: ProjectContextDep, service: ProjectServiceDep) -> ProjectResponse:
    return _project(await service.view(ctx))


@router.patch("/{project_id}", summary="Rename or describe a project", responses=_WRITE)
async def update_project(
    data: ProjectUpdate, ctx: ProjectContextDep, service: ProjectServiceDep
) -> ProjectResponse:
    await service.update(ctx, data)
    return _project(await service.view(ctx))


@router.post("/{project_id}/archive", summary="Archive a project", responses=_WRITE)
async def archive_project(ctx: ProjectContextDep, service: ProjectServiceDep) -> ProjectResponse:
    """An archived project is read-only until it is restored."""
    await service.set_archived(ctx, True)
    return _project(await service.view(ctx))


@router.post("/{project_id}/restore", summary="Restore an archived project", responses=_WRITE)
async def restore_project(ctx: ProjectContextDep, service: ProjectServiceDep) -> ProjectResponse:
    await service.set_archived(ctx, False)
    return _project(await service.view(ctx))


@router.delete(
    "/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a project (admin, owner)",
    responses=_WRITE,
)
async def delete_project(
    ctx: ProjectContextDep,
    service: ProjectServiceDep,
    confirm_name: Annotated[str, Query(max_length=120, description="The project's exact name.")],
) -> None:
    """Safeguards: the project must be archived first, `confirm_name` must match, and
    no test may be running. Its stored credentials are destroyed."""
    await service.delete(ctx, confirm_name=confirm_name)


# --- project members ----------------------------------------------------------------------


def _member(row: tuple[Any, Any, str]) -> ProjectMemberResponse:
    member, user, organization_role = row
    return ProjectMemberResponse(
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=member.role,
        organization_role=organization_role,
        added_at=member.created_at,
    )


@router.get("/{project_id}/members", summary="List project members", responses=_READ)
async def list_project_members(
    ctx: ProjectContextDep, service: ProjectServiceDep
) -> list[ProjectMemberResponse]:
    return [_member(row) for row in await service.list_members(ctx)]


@router.post(
    "/{project_id}/members",
    status_code=status.HTTP_201_CREATED,
    summary="Add an organization member to the project",
    responses=_WRITE,
)
async def add_project_member(
    data: ProjectMemberAdd, ctx: ProjectContextDep, service: ProjectServiceDep
) -> list[ProjectMemberResponse]:
    await service.add_member(ctx, user_id=data.user_id, role=data.role)
    return [_member(row) for row in await service.list_members(ctx)]


@router.patch(
    "/{project_id}/members/{user_id}", summary="Change a project member's role", responses=_WRITE
)
async def update_project_member(
    user_id: uuid.UUID,
    data: ProjectMemberUpdate,
    ctx: ProjectContextDep,
    service: ProjectServiceDep,
) -> list[ProjectMemberResponse]:
    await service.change_member_role(ctx, user_id=user_id, role=data.role)
    return [_member(row) for row in await service.list_members(ctx)]


@router.delete(
    "/{project_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a project member, or leave",
    responses=_WRITE,
)
async def remove_project_member(
    user_id: uuid.UUID, ctx: ProjectContextDep, service: ProjectServiceDep
) -> None:
    await service.remove_member(ctx, user_id=user_id)


# --- applications --------------------------------------------------------------------------

_APPS = "/{project_id}/applications"
_APP = _APPS + "/{application_id}"


@router.get(_APPS, summary="List applications", tags=["applications"], responses=_READ)
async def list_applications(
    ctx: ProjectContextDep, service: ApplicationServiceDep
) -> list[ApplicationResponse]:
    return [_application(a, count) for a, count in await service.list_applications(ctx)]


@router.post(
    _APPS,
    status_code=status.HTTP_201_CREATED,
    summary="Register an application",
    tags=["applications"],
    responses=_WRITE,
)
async def create_application(
    data: ApplicationCreate, ctx: ProjectContextDep, service: ApplicationServiceDep
) -> ApplicationResponse:
    return _application(await service.create_application(ctx, data), 0)


@router.get(_APP, summary="Get an application", tags=["applications"], responses=_READ)
async def get_application_route(
    ctx: ProjectContextDep, application: ApplicationDep, service: ApplicationServiceDep
) -> ApplicationResponse:
    return _application(application, await service.environment_count(ctx, application))


@router.patch(_APP, summary="Update an application", tags=["applications"], responses=_WRITE)
async def update_application(
    data: ApplicationUpdate,
    ctx: ProjectContextDep,
    application: ApplicationDep,
    service: ApplicationServiceDep,
) -> ApplicationResponse:
    application = await service.update_application(ctx, application, data)
    return _application(application, await service.environment_count(ctx, application))


@router.delete(
    _APP,
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an application",
    tags=["applications"],
    responses=_WRITE,
)
async def delete_application(
    ctx: ProjectContextDep, application: ApplicationDep, service: ApplicationServiceDep
) -> None:
    """Also removes its environments and destroys their stored credentials."""
    await service.delete_application(ctx, application)


# --- environments --------------------------------------------------------------------------

_ENVS = _APP + "/environments"
_ENV = _ENVS + "/{environment_id}"


@router.get(_ENVS, summary="List environments", tags=["environments"], responses=_READ)
async def list_environments(
    ctx: ProjectContextDep, application: ApplicationDep, service: ApplicationServiceDep
) -> list[EnvironmentResponse]:
    return [_environment(view) for view in await service.list_environments(ctx, application)]


@router.post(
    _ENVS,
    status_code=status.HTTP_201_CREATED,
    summary="Add an environment",
    tags=["environments"],
    responses=_WRITE,
)
async def create_environment(
    data: EnvironmentCreate,
    ctx: ProjectContextDep,
    application: ApplicationDep,
    service: ApplicationServiceDep,
) -> EnvironmentResponse:
    """Every URL is validated: syntax, the application's allowed domains, then DNS, where
    each resolved address must be public unless `allow_private_network` is set by an admin."""
    environment = await service.create_environment(ctx, application, data)
    return _environment(await service.view_environment(ctx, environment))


@router.get(_ENV, summary="Get an environment", tags=["environments"], responses=_READ)
async def get_environment_route(
    ctx: ProjectContextDep, environment: EnvironmentDep, service: ApplicationServiceDep
) -> EnvironmentResponse:
    return _environment(await service.view_environment(ctx, environment))


@router.patch(_ENV, summary="Update an environment", tags=["environments"], responses=_WRITE)
async def update_environment(
    data: EnvironmentUpdate,
    ctx: ProjectContextDep,
    application: ApplicationDep,
    environment: EnvironmentDep,
    service: ApplicationServiceDep,
) -> EnvironmentResponse:
    environment = await service.update_environment(ctx, application, environment, data)
    return _environment(await service.view_environment(ctx, environment))


@router.delete(
    _ENV,
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an environment",
    tags=["environments"],
    responses=_WRITE,
)
async def delete_environment(
    ctx: ProjectContextDep, environment: EnvironmentDep, service: ApplicationServiceDep
) -> None:
    await service.delete_environment(ctx, environment)


@router.post(
    _ENV + "/access-check",
    summary="Check that the environment can be reached safely",
    tags=["environments"],
    responses={**_WRITE, 429: _ERR},
)
async def check_environment_access(
    ctx: ProjectContextDep, environment: EnvironmentDep, service: ApplicationServiceDep
) -> AccessCheckResponse:
    """Requests the base URL and follows redirects one at a time; each hop must be an
    allowed origin that resolves to permitted addresses. Reports status codes only."""
    result = await service.check_access(ctx, environment)
    return AccessCheckResponse(
        reachable=result.reachable,
        final_url=result.final_url,
        status=result.status,
        duration_ms=result.duration_ms,
        error_code=result.error_code,
        error=result.error,
        hops=[
            AccessCheckHop(
                url=hop.url,
                status=hop.status,
                blocked_code=hop.blocked_code,
                blocked_reason=hop.blocked_reason,
            )
            for hop in result.hops
        ],
    )


# --- credentials ---------------------------------------------------------------------------

_CREDS = _ENV + "/credentials"
_CRED = _CREDS + "/{credential_id}"


@router.get(_CREDS, summary="List credentials (metadata)", tags=["credentials"], responses=_READ)
async def list_credentials(
    ctx: ProjectContextDep, environment: EnvironmentDep, service: CredentialServiceDep
) -> list[CredentialResponse]:
    """Names, kinds and field names. Secret values are never returned by any endpoint."""
    return [_credential(c) for c in await service.list(ctx, environment)]


@router.post(
    _CREDS,
    status_code=status.HTTP_201_CREATED,
    summary="Store a test credential",
    tags=["credentials"],
    responses=_WRITE,
)
async def create_credential(
    data: CredentialCreate,
    ctx: ProjectContextDep,
    environment: EnvironmentDep,
    service: CredentialServiceDep,
) -> CredentialResponse:
    """The secret is encrypted before it is stored, or only a reference to it is kept."""
    return _credential(await service.create(ctx, environment, data))


@router.get(_CRED, summary="Get a credential (metadata)", tags=["credentials"], responses=_READ)
async def get_credential_route(credential: CredentialDep) -> CredentialResponse:
    return _credential(credential)


@router.patch(
    _CRED, summary="Update or rotate a credential", tags=["credentials"], responses=_WRITE
)
async def update_credential(
    data: CredentialUpdate,
    ctx: ProjectContextDep,
    environment: EnvironmentDep,
    credential: CredentialDep,
    service: CredentialServiceDep,
) -> CredentialResponse:
    return _credential(await service.update(ctx, environment, credential, data))


@router.delete(
    _CRED,
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a credential",
    tags=["credentials"],
    responses=_WRITE,
)
async def delete_credential(
    ctx: ProjectContextDep,
    environment: EnvironmentDep,
    credential: CredentialDep,
    service: CredentialServiceDep,
) -> None:
    await service.delete(ctx, environment, credential)
