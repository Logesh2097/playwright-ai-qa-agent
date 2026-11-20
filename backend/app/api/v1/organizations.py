"""Organizations, members and the audit log.

Every route below `/{organization_id}` depends on `OrgContextDep`: the caller
must be an active member, otherwise the answer is 404.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query, status

from app.api.deps import CurrentUserDep, OrganizationServiceDep, OrgContextDep
from app.core.security.permissions import Role
from app.models import Organization, OrganizationMember, User
from app.schemas.common import Page, ProblemDetail
from app.schemas.organizations import (
    AuditLogResponse,
    InvitationCreate,
    MemberResponse,
    MemberUpdate,
    OrganizationCreate,
    OrganizationResponse,
    OrganizationUpdate,
)

router = APIRouter(prefix="/organizations", tags=["organizations"])

Responses = dict[int | str, dict[str, Any]]
_ERR: dict[str, Any] = {"model": ProblemDetail}
_AUTH: Responses = {401: _ERR, 404: _ERR}
_AUTHZ: Responses = {401: _ERR, 403: _ERR, 404: _ERR}


def _organization(organization: Organization, role: str) -> OrganizationResponse:
    return OrganizationResponse(
        id=organization.id,
        name=organization.name,
        slug=organization.slug,
        role=Role(role),
        created_at=organization.created_at,
    )


def _member(member: OrganizationMember, user: User | None) -> MemberResponse:
    return MemberResponse(
        id=member.id,
        user_id=member.user_id,
        email=user.email if user else (member.invited_email or ""),
        full_name=user.full_name if user else None,
        role=Role(member.role),
        status=member.status,
        created_at=member.created_at,
    )


@router.get("", summary="Organizations the caller belongs to", responses={401: _ERR})
async def list_organizations(
    current: CurrentUserDep, service: OrganizationServiceDep
) -> list[OrganizationResponse]:
    rows = await service.list_for_user(current.user)
    return [_organization(organization, membership.role) for organization, membership in rows]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create an organization",
    responses={401: _ERR, 422: _ERR},
)
async def create_organization(
    data: OrganizationCreate, current: CurrentUserDep, service: OrganizationServiceDep
) -> OrganizationResponse:
    """The caller becomes the owner."""
    organization = await service.create(current.user, data.name)
    return _organization(organization, Role.OWNER.value)


@router.get("/{organization_id}", summary="Get an organization", responses=_AUTH)
async def get_organization(ctx: OrgContextDep) -> OrganizationResponse:
    return _organization(ctx.organization, ctx.membership.role)


@router.patch(
    "/{organization_id}", summary="Rename an organization (admin, owner)", responses=_AUTHZ
)
async def update_organization(
    data: OrganizationUpdate, ctx: OrgContextDep, service: OrganizationServiceDep
) -> OrganizationResponse:
    organization = await service.update(ctx, name=data.name)
    return _organization(organization, ctx.membership.role)


@router.delete(
    "/{organization_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an organization (owner)",
    responses=_AUTHZ,
)
async def delete_organization(ctx: OrgContextDep, service: OrganizationServiceDep) -> None:
    """Soft delete: the organization disappears for every member."""
    await service.delete(ctx)


@router.get("/{organization_id}/members", summary="List members and invitations", responses=_AUTH)
async def list_members(ctx: OrgContextDep, service: OrganizationServiceDep) -> list[MemberResponse]:
    return [_member(member, user) for member, user in await service.list_members(ctx)]


@router.post(
    "/{organization_id}/invitations",
    status_code=status.HTTP_201_CREATED,
    summary="Invite someone by email (admin, owner)",
    responses={**_AUTHZ, 409: _ERR},
)
async def invite_member(
    data: InvitationCreate, ctx: OrgContextDep, service: OrganizationServiceDep
) -> MemberResponse:
    """The invitation link is emailed; it is never returned by the API. A role above
    the caller's own cannot be granted."""
    invitation = await service.invite(ctx, email=data.email, role=data.role)
    return _member(invitation, None)


@router.patch(
    "/{organization_id}/members/{member_id}",
    summary="Change a member's role (admin, owner)",
    responses={**_AUTHZ, 409: _ERR},
)
async def update_member(
    member_id: uuid.UUID,
    data: MemberUpdate,
    ctx: OrgContextDep,
    service: OrganizationServiceDep,
) -> MemberResponse:
    member, user = await service.change_role(ctx, member_id, data.role)
    return _member(member, user)


@router.delete(
    "/{organization_id}/members/{member_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a member, cancel an invitation, or leave",
    responses={**_AUTHZ, 409: _ERR},
)
async def remove_member(
    member_id: uuid.UUID, ctx: OrgContextDep, service: OrganizationServiceDep
) -> None:
    """Admins and owners may remove others; anyone may remove themselves. The last
    owner cannot be removed."""
    await service.remove_member(ctx, member_id)


@router.get(
    "/{organization_id}/audit-logs",
    summary="Audit trail, newest first (admin, owner)",
    responses=_AUTHZ,
)
async def list_audit_logs(
    ctx: OrgContextDep,
    service: OrganizationServiceDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[
        uuid.UUID | None, Query(description="`next_cursor` of the previous page.")
    ] = None,
    action: Annotated[str | None, Query(max_length=100)] = None,
    actor_id: uuid.UUID | None = None,
) -> Page[AuditLogResponse]:
    rows, next_cursor = await service.audit_page(
        ctx, limit=limit, cursor=cursor, action=action, actor_id=actor_id
    )
    return Page[AuditLogResponse](
        items=[AuditLogResponse.model_validate(row) for row in rows],
        next_cursor=str(next_cursor) if next_cursor else None,
    )
