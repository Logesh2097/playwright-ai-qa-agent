"""Request and response bodies for projects, applications, environments and credentials."""

import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from app.schemas.common import ApiModel

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]
UrlText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]

ProjectStatus = Literal["active", "archived"]
ProjectRole = Literal["manager", "member"]
ApplicationType = Literal["web", "api"]
ApplicationStatus = Literal["active", "archived"]
AuthMethod = Literal["none", "form_login", "basic_auth", "bearer_token", "api_key", "sso", "other"]
EnvironmentKind = Literal["local", "development", "testing", "staging", "production", "other"]
EnvironmentStatus = Literal["active", "disabled"]
BrowserAction = Literal[
    "navigate", "read", "click", "type", "select", "upload", "download", "api_read", "api_write"
]
OriginPurpose = Literal["app", "api", "auth_provider", "asset"]
CredentialKind = Literal[
    "username_password",
    "bearer_token",
    "api_key_header",
    "basic_auth",
    "cookie",
    "totp_secret",
    "secret_value",
]
BrowserName = Literal["chromium", "firefox", "webkit"]

_VARIABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_EXTERNAL_REF = re.compile(r"^env:[A-Z_][A-Z0-9_]{0,127}$")


# --- projects ------------------------------------------------------------------------


class ProjectCreate(ApiModel):
    name: Name
    description: Description | None = None


class ProjectUpdate(ApiModel):
    name: Name | None = None
    description: Description | None = None


class ProjectResponse(ApiModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    slug: str
    description: str | None
    status: ProjectStatus
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    member_count: int
    application_count: int
    my_role: ProjectRole | None = Field(
        description="The caller's role in the project; null for organization admins who are not members."
    )
    can_manage: bool = Field(description="Whether the caller may edit the project and its members.")


class ProjectMemberAdd(ApiModel):
    user_id: uuid.UUID
    role: ProjectRole = "member"


class ProjectMemberUpdate(ApiModel):
    role: ProjectRole


class ProjectMemberResponse(ApiModel):
    user_id: uuid.UUID
    email: str
    full_name: str
    role: ProjectRole
    organization_role: str
    added_at: datetime


# --- applications --------------------------------------------------------------------


class Viewport(ApiModel):
    width: int = Field(ge=240, le=7680)
    height: int = Field(ge=240, le=4320)


class BrowserConfig(ApiModel):
    browser: BrowserName = "chromium"
    viewport: Viewport = Viewport(width=1440, height=900)
    locale: Annotated[str, StringConstraints(pattern=r"^[a-z]{2,3}(-[A-Z]{2})?$")] | None = None
    timezone: Annotated[str, StringConstraints(max_length=64)] | None = None


class ApplicationCreate(ApiModel):
    name: Name
    description: Description | None = None
    base_url: UrlText
    type: ApplicationType = "web"
    framework: ShortText | None = Field(
        default=None, description="e.g. 'Vue 3 + Laravel'. Free text; helps the agent later."
    )
    auth_method: AuthMethod = "none"
    default_browser_config: BrowserConfig = BrowserConfig()
    allowed_domains: list[ShortText] = Field(
        default_factory=list,
        max_length=50,
        description="Host names this application may be reached on; each covers its subdomains. "
        "Defaults to the host of base_url.",
    )


class ApplicationUpdate(ApiModel):
    name: Name | None = None
    description: Description | None = None
    base_url: UrlText | None = None
    type: ApplicationType | None = None
    framework: ShortText | None = None
    auth_method: AuthMethod | None = None
    default_browser_config: BrowserConfig | None = None
    allowed_domains: list[ShortText] | None = Field(default=None, min_length=1, max_length=50)
    status: ApplicationStatus | None = None


class ApplicationResponse(ApiModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    description: str | None
    base_url: str
    type: ApplicationType
    framework: str | None
    auth_method: AuthMethod
    default_browser_config: dict[str, Any]
    allowed_domains: list[str]
    status: ApplicationStatus
    environment_count: int
    created_at: datetime
    updated_at: datetime


# --- environments --------------------------------------------------------------------


class AllowedOriginInput(ApiModel):
    url: UrlText = Field(description="Origin as a URL, e.g. https://auth.example.com")
    purpose: OriginPurpose = "app"


class AllowedOriginResponse(ApiModel):
    origin: str
    scheme: str
    host: str
    port: int
    purpose: OriginPurpose


class EnvironmentVariable(ApiModel):
    """A non-secret setting, or a pointer to a credential of the same environment."""

    name: str
    value: Annotated[str, StringConstraints(max_length=2000)] | None = None
    secret_ref: ShortText | None = Field(
        default=None, description="Name of a credential in this environment."
    )

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not _VARIABLE_NAME.fullmatch(value):
            raise ValueError("use letters, digits and underscores, starting with a letter")
        return value

    @model_validator(mode="after")
    def _one_of(self) -> Self:
        if (self.value is None) == (self.secret_ref is None):
            raise ValueError("give either value or secret_ref")
        return self


class EnvironmentAuthConfig(ApiModel):
    method: AuthMethod = "none"
    login_url: UrlText | None = None
    default_credential: ShortText | None = Field(
        default=None, description="Name of the credential tests use unless a case says otherwise."
    )
    notes: Annotated[str, StringConstraints(max_length=4000)] | None = None


class EnvironmentCreate(ApiModel):
    name: Name
    kind: EnvironmentKind
    base_url: UrlText
    api_base_url: UrlText | None = None
    additional_origins: list[AllowedOriginInput] = Field(default_factory=list, max_length=30)
    variables: list[EnvironmentVariable] = Field(default_factory=list, max_length=100)
    auth_config: EnvironmentAuthConfig = EnvironmentAuthConfig()
    allowed_actions: list[BrowserAction] | None = Field(
        default=None,
        description="Defaults to everything, or to read-only actions when is_read_only.",
    )
    is_read_only: bool | None = Field(default=None, description="Defaults to true for production.")
    requires_approval: bool | None = Field(
        default=None, description="Defaults to true for production."
    )
    allow_private_network: bool = Field(
        default=False,
        description="Permit loopback and private addresses. Admins only; audit-logged.",
    )


class EnvironmentUpdate(ApiModel):
    name: Name | None = None
    kind: EnvironmentKind | None = None
    base_url: UrlText | None = None
    api_base_url: UrlText | None = None
    additional_origins: list[AllowedOriginInput] | None = Field(default=None, max_length=30)
    variables: list[EnvironmentVariable] | None = Field(default=None, max_length=100)
    auth_config: EnvironmentAuthConfig | None = None
    allowed_actions: list[BrowserAction] | None = None
    is_read_only: bool | None = None
    requires_approval: bool | None = None
    allow_private_network: bool | None = None
    status: EnvironmentStatus | None = None


class EnvironmentResponse(ApiModel):
    id: uuid.UUID
    application_id: uuid.UUID
    name: str
    kind: EnvironmentKind
    base_url: str
    api_base_url: str | None
    allowed_origins: list[AllowedOriginResponse]
    variables: list[EnvironmentVariable]
    auth_config: EnvironmentAuthConfig
    allowed_actions: list[BrowserAction]
    is_read_only: bool
    requires_approval: bool
    allow_private_network: bool
    status: EnvironmentStatus
    credential_count: int
    created_at: datetime
    updated_at: datetime


class AccessCheckHop(ApiModel):
    url: str
    status: int | None
    blocked_code: str | None = None
    blocked_reason: str | None = None


class AccessCheckResponse(ApiModel):
    """Result of contacting the environment. Response bodies are never included."""

    reachable: bool
    final_url: str | None
    status: int | None
    duration_ms: int
    error_code: str | None
    error: str | None
    hops: list[AccessCheckHop]


# --- credentials ---------------------------------------------------------------------

SecretFields = dict[
    Annotated[str, StringConstraints(pattern=r"^[a-z_]{1,40}$")],
    Annotated[str, StringConstraints(min_length=1, max_length=8192)],
]


def _checked_recipe(value: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """An empty recipe is allowed (the credential then cannot be used to sign in)."""
    if not value:
        return value
    from app.browser.login import LoginRecipe

    return LoginRecipe.model_validate(value).dump()


class CredentialCreate(ApiModel):
    name: Name
    role_label: ShortText = Field(description="What tests call this account, e.g. 'customer'.")
    kind: CredentialKind
    secret: SecretFields | None = Field(
        default=None,
        description="Secret fields for this kind. Write-only: never returned by any endpoint.",
    )
    external_ref: str | None = Field(
        default=None,
        description="Instead of `secret`: where the value lives, e.g. env:STAGING_CUSTOMER_PASSWORD.",
    )
    is_test_account: bool = Field(
        description="Must be true: confirms this is a dedicated test account, not a real user's."
    )
    setup_instructions: Annotated[str, StringConstraints(max_length=4000)] | None = None
    login_recipe: list[dict[str, Any]] = Field(default_factory=list, max_length=50)

    @field_validator("login_recipe")
    @classmethod
    def _valid_recipe(cls, value: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        return _checked_recipe(value)

    @field_validator("external_ref")
    @classmethod
    def _valid_reference(cls, value: str | None) -> str | None:
        if value is not None and not _EXTERNAL_REF.fullmatch(value):
            raise ValueError("must look like env:VARIABLE_NAME")
        return value

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        if (self.secret is None) == (self.external_ref is None):
            raise ValueError("give either secret or external_ref")
        return self


class CredentialUpdate(ApiModel):
    name: Name | None = None
    role_label: ShortText | None = None
    secret: SecretFields | None = Field(default=None, description="Replaces the stored secret.")
    external_ref: str | None = None
    setup_instructions: Annotated[str, StringConstraints(max_length=4000)] | None = None
    login_recipe: list[dict[str, Any]] | None = Field(default=None, max_length=50)

    @field_validator("login_recipe")
    @classmethod
    def _valid_recipe(cls, value: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        return _checked_recipe(value)

    @field_validator("external_ref")
    @classmethod
    def _valid_reference(cls, value: str | None) -> str | None:
        if value is not None and not _EXTERNAL_REF.fullmatch(value):
            raise ValueError("must look like env:VARIABLE_NAME")
        return value

    @model_validator(mode="after")
    def _not_both(self) -> Self:
        if self.secret is not None and self.external_ref is not None:
            raise ValueError("give secret or external_ref, not both")
        return self


class CredentialResponse(ApiModel):
    """Metadata only. There is no field that carries a secret value."""

    id: uuid.UUID
    environment_id: uuid.UUID
    name: str
    role_label: str
    kind: CredentialKind
    source: Literal["stored", "reference"]
    field_names: list[str] = Field(description="Which fields are stored, e.g. username, password.")
    external_ref: str | None
    is_test_account: bool
    setup_instructions: str | None
    login_recipe: list[dict[str, Any]]
    last_rotated_at: datetime | None
    created_at: datetime
    updated_at: datetime
