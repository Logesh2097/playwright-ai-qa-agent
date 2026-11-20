"""Request and response bodies for organizations, members and the audit log."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import EmailStr, Field, StringConstraints, field_validator

from app.core.security.permissions import Role
from app.schemas.common import ApiModel

OrgName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=120)]


class OrganizationCreate(ApiModel):
    name: OrgName


class OrganizationUpdate(ApiModel):
    name: OrgName


class OrganizationResponse(ApiModel):
    id: uuid.UUID
    name: str
    slug: str
    role: Role = Field(description="The caller's role in this organization.")
    created_at: datetime


class InvitationCreate(ApiModel):
    email: EmailStr = Field(max_length=254)
    role: Role = Role.DEVELOPER


class MemberUpdate(ApiModel):
    role: Role


class MemberResponse(ApiModel):
    id: uuid.UUID
    user_id: uuid.UUID | None
    email: str
    full_name: str | None
    role: Role
    status: Literal["invited", "active", "suspended"]
    created_at: datetime


class AuditLogResponse(ApiModel):
    id: uuid.UUID
    created_at: datetime
    actor_type: str
    actor_id: uuid.UUID | None
    action: str
    resource_type: str | None
    resource_id: uuid.UUID | None
    request_id: str | None
    ip: str | None
    changes: dict[str, Any]

    @field_validator("ip", mode="before")
    @classmethod
    def _ip_to_text(cls, value: object) -> object:
        # The driver returns INET columns as ipaddress objects.
        return None if value is None else str(value)
