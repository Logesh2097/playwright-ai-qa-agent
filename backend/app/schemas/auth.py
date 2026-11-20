"""Request and response bodies for authentication."""

import uuid
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import EmailStr, Field, StringConstraints, model_validator

from app.core.security.permissions import Role
from app.schemas.common import ApiModel

# Length limits only; the password policy itself is applied by the service so that
# a weak password is reported as `weak_password`, with reasons.
PasswordInput = Annotated[str, StringConstraints(min_length=1, max_length=128)]
NameInput = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
TokenInput = Annotated[str, StringConstraints(min_length=20, max_length=200)]


class RegisterRequest(ApiModel):
    email: EmailStr = Field(max_length=254)
    password: PasswordInput
    full_name: NameInput
    organization_name: NameInput | None = Field(
        default=None, description="Name of the organization to create. Omit when joining by invite."
    )
    invite_token: TokenInput | None = Field(
        default=None, description="Invitation token. The email must match the invitation."
    )

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.organization_name is None) == (self.invite_token is None):
            raise ValueError("provide either organization_name or invite_token")
        return self


class LoginRequest(ApiModel):
    email: EmailStr = Field(max_length=254)
    password: PasswordInput


class ChangePasswordRequest(ApiModel):
    current_password: PasswordInput
    new_password: PasswordInput


class ForgotPasswordRequest(ApiModel):
    email: EmailStr = Field(max_length=254)


class ResetPasswordRequest(ApiModel):
    token: TokenInput
    new_password: PasswordInput


class AcceptInvitationRequest(ApiModel):
    token: TokenInput


class UserResponse(ApiModel):
    id: uuid.UUID
    email: str
    full_name: str
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None


class MembershipSummary(ApiModel):
    organization_id: uuid.UUID
    organization_name: str
    organization_slug: str
    role: Role


class TokenResponse(ApiModel):
    """The refresh token is not in the body: it is set as an HttpOnly cookie."""

    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - a scheme name
    expires_in: int = Field(description="Lifetime of the access token in seconds.")


class SessionResponse(TokenResponse):
    user: UserResponse
    memberships: list[MembershipSummary]


class MeResponse(ApiModel):
    user: UserResponse
    memberships: list[MembershipSummary]


class AcceptedResponse(ApiModel):
    """Returned whether or not the account exists, so registered emails are not revealed."""

    status: Literal["accepted"] = "accepted"
