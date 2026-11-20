"""FastAPI dependencies. Route handlers get their collaborators from here.

Authorization lives here and in the services, never in the frontend: every
organization-scoped route depends on `OrgContextDep`, which verifies membership
and sets the database's row-level security context before any query runs.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import BadRequestError, NotFoundError, UnauthenticatedError
from app.core.redis import RedisClient
from app.core.security.tokens import decode_access_token
from app.core.security.url_safety import AccessValidator
from app.db.context import set_current_org, set_current_user
from app.models import User
from app.repositories.identity import MembershipRepository, OrganizationRepository, UserRepository
from app.services.access_validation import AccessProbe
from app.services.applications import ApplicationService
from app.services.audit import AuditService, RequestMeta
from app.services.auth import AuthService, revoked_session_key
from app.services.browser_checks import BrowserCheckService
from app.services.credentials import CredentialService
from app.services.email import EmailSender
from app.services.evidence_access import EvidenceAccess
from app.services.executions import ExecutionService
from app.services.health import HealthService, postgres_check, redis_check
from app.services.instructions import InstructionService
from app.services.organizations import OrganizationService, OrgContext
from app.services.projects import ProjectContext, ProjectService
from app.services.quality import QualityService
from app.services.reporting import ReportingService
from app.services.test_cases import TestCaseService
from app.services.test_data import TestDataService
from app.services.test_suites import SuiteService
from app.services.workflows import WorkflowService

_bearer = HTTPBearer(auto_error=False, description="Access token from /auth/login.")


def get_app_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_redis(request: Request) -> RedisClient:
    redis: RedisClient = request.app.state.redis
    return redis


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session and one transaction per request.

    Commits when the handler returns, rolls back when it raises. Declared with
    function scope so the commit happens before the response is sent: a client
    never receives a success for work that failed to commit.
    """
    async with request.app.state.session_factory() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise
        else:
            await session.commit()


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
RedisDep = Annotated[RedisClient, Depends(get_redis)]
DbSessionDep = Annotated[AsyncSession, Depends(get_db_session, scope="function")]


def get_health_service(request: Request, settings: SettingsDep) -> HealthService:
    return HealthService(
        {
            "postgres": postgres_check(request.app.state.db_engine),
            "redis": redis_check(request.app.state.redis),
        },
        timeout_s=settings.readiness_timeout_s,
    )


HealthServiceDep = Annotated[HealthService, Depends(get_health_service)]


# --- request metadata and services ---------------------------------------------


def get_request_meta(request: Request) -> RequestMeta:
    return RequestMeta(
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


RequestMetaDep = Annotated[RequestMeta, Depends(get_request_meta)]


def get_audit_service(request: Request, meta: RequestMetaDep) -> AuditService:
    return AuditService(request.app.state.session_factory, meta)


def get_email_sender(request: Request) -> EmailSender:
    sender: EmailSender = request.app.state.email_sender
    return sender


AuditServiceDep = Annotated[AuditService, Depends(get_audit_service)]
EmailSenderDep = Annotated[EmailSender, Depends(get_email_sender)]


def get_auth_service(
    session: DbSessionDep,
    redis: RedisDep,
    settings: SettingsDep,
    audit: AuditServiceDep,
    email: EmailSenderDep,
    meta: RequestMetaDep,
) -> AuthService:
    return AuthService(
        session=session, redis=redis, settings=settings, audit=audit, email=email, meta=meta
    )


def get_organization_service(
    session: DbSessionDep, settings: SettingsDep, audit: AuditServiceDep, email: EmailSenderDep
) -> OrganizationService:
    return OrganizationService(session=session, settings=settings, audit=audit, email=email)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
OrganizationServiceDep = Annotated[OrganizationService, Depends(get_organization_service)]


# --- authentication ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CurrentUser:
    user: User
    session_id: uuid.UUID


async def get_current_user(
    session: DbSessionDep,
    redis: RedisDep,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> CurrentUser:
    challenge = {"WWW-Authenticate": "Bearer"}
    if credentials is None:
        raise UnauthenticatedError("Authentication is required.", headers=challenge)
    claims = decode_access_token(
        credentials.credentials,
        secret=settings.jwt_secret_key.get_secret_value(),
        issuer=settings.app_name,
    )
    # Logout, password change and token-reuse detection revoke the session at once.
    if await redis.exists(revoked_session_key(claims.session_id)):
        raise UnauthenticatedError(
            "The session has ended. Please sign in again.",
            code="session_revoked",
            headers=challenge,
        )
    user = await UserRepository(session).get(claims.user_id)
    if user is None or not user.is_active:
        raise UnauthenticatedError("Authentication is required.", headers=challenge)
    await set_current_user(session, user.id)
    return CurrentUser(user=user, session_id=claims.session_id)


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


def require_csrf_header(
    x_requested_with: Annotated[str | None, Header(alias="X-Requested-With")] = None,
) -> None:
    """Guard for endpoints authenticated by the refresh cookie.

    A cross-site form or image request cannot add a custom header, and the cookie
    is SameSite=Strict, so requiring this header closes the CSRF route.
    """
    if not x_requested_with:
        raise BadRequestError(
            "The X-Requested-With header is required.", code="csrf_header_missing"
        )


CsrfGuard = Depends(require_csrf_header)


# --- organization scope -----------------------------------------------------------


async def get_org_context(
    organization_id: uuid.UUID, current: CurrentUserDep, session: DbSessionDep
) -> OrgContext:
    """Verify the caller belongs to the organization in the path.

    A non-member receives 404, not 403: the existence of other tenants'
    organizations is not disclosed.
    """
    not_found = NotFoundError("Organization not found.")
    membership = await MembershipRepository(session, organization_id).get_active_for_user(
        current.user.id
    )
    if membership is None:
        raise not_found
    await set_current_org(session, organization_id)
    organization = await OrganizationRepository(session).get_active(organization_id)
    if organization is None:
        raise not_found
    return OrgContext(user=current.user, organization=organization, membership=membership)


OrgContextDep = Annotated[OrgContext, Depends(get_org_context)]


# --- project scope ------------------------------------------------------------------


def get_project_service(session: DbSessionDep, audit: AuditServiceDep) -> ProjectService:
    return ProjectService(session=session, audit=audit)


ProjectServiceDep = Annotated[ProjectService, Depends(get_project_service)]


async def get_project_context(
    project_id: uuid.UUID, org: OrgContextDep, service: ProjectServiceDep
) -> ProjectContext:
    """The caller must be able to see the project: a member, or an organization admin."""
    return await service.resolve(org, project_id)


ProjectContextDep = Annotated[ProjectContext, Depends(get_project_context)]


def get_application_service(
    request: Request,
    session: DbSessionDep,
    audit: AuditServiceDep,
    redis: RedisDep,
    settings: SettingsDep,
) -> ApplicationService:
    validator: AccessValidator = request.app.state.access_validator
    return ApplicationService(
        session=session,
        audit=audit,
        validator=validator,
        probe=AccessProbe(
            validator,
            timeout_s=settings.access_probe_timeout_s,
            max_redirects=settings.access_probe_max_redirects,
            transport=request.app.state.access_probe_transport,
        ),
        redis=redis,
        settings=settings,
    )


def get_credential_service(
    request: Request, session: DbSessionDep, audit: AuditServiceDep
) -> CredentialService:
    return CredentialService(
        session=session, audit=audit, cipher=request.app.state.credential_cipher
    )


def get_instruction_service(
    request: Request,
    session: DbSessionDep,
    audit: AuditServiceDep,
    redis: RedisDep,
    settings: SettingsDep,
) -> InstructionService:
    return InstructionService(
        session=session,
        audit=audit,
        jobs=request.app.state.job_queue,
        gateway=request.app.state.llm_gateway,
        redis=redis,
        settings=settings,
    )


def get_test_case_service(
    request: Request, session: DbSessionDep, audit: AuditServiceDep, settings: SettingsDep
) -> TestCaseService:
    return TestCaseService(
        session=session,
        audit=audit,
        jobs=request.app.state.job_queue,
        gateway=request.app.state.llm_gateway,
        settings=settings,
    )


def get_suite_service(session: DbSessionDep, audit: AuditServiceDep) -> SuiteService:
    return SuiteService(session=session, audit=audit)


TestCaseServiceDep = Annotated[TestCaseService, Depends(get_test_case_service)]
SuiteServiceDep = Annotated[SuiteService, Depends(get_suite_service)]
InstructionServiceDep = Annotated[InstructionService, Depends(get_instruction_service)]
ApplicationServiceDep = Annotated[ApplicationService, Depends(get_application_service)]
CredentialServiceDep = Annotated[CredentialService, Depends(get_credential_service)]


def get_browser_check_service(
    request: Request,
    session: DbSessionDep,
    audit: AuditServiceDep,
    redis: RedisDep,
    settings: SettingsDep,
) -> BrowserCheckService:
    return BrowserCheckService(
        session=session,
        audit=audit,
        jobs=request.app.state.job_queue,
        redis=redis,
        settings=settings,
    )


def get_execution_service(
    request: Request,
    session: DbSessionDep,
    audit: AuditServiceDep,
    redis: RedisDep,
    settings: SettingsDep,
) -> ExecutionService:
    return ExecutionService(
        session=session,
        audit=audit,
        jobs=request.app.state.job_queue,
        redis=redis,
        settings=settings,
    )


def get_quality_service(
    request: Request,
    session: DbSessionDep,
    audit: AuditServiceDep,
    cases: Annotated[TestCaseService, Depends(get_test_case_service)],
) -> QualityService:
    return QualityService(
        session=session, audit=audit, gateway=request.app.state.llm_gateway, cases=cases
    )


def get_evidence_access(
    session: DbSessionDep, audit: AuditServiceDep, settings: SettingsDep
) -> EvidenceAccess:
    return EvidenceAccess(session=session, audit=audit, settings=settings)


def get_reporting_service(
    session: DbSessionDep, audit: AuditServiceDep, settings: SettingsDep
) -> ReportingService:
    return ReportingService(session=session, audit=audit, settings=settings)


def get_test_data_service(
    session: DbSessionDep, audit: AuditServiceDep, settings: SettingsDep
) -> TestDataService:
    return TestDataService(session=session, audit=audit, settings=settings)


def get_workflow_service(
    request: Request,
    session: DbSessionDep,
    audit: AuditServiceDep,
    redis: RedisDep,
    settings: SettingsDep,
) -> WorkflowService:
    return WorkflowService(
        session=session,
        audit=audit,
        jobs=request.app.state.job_queue,
        redis=redis,
        settings=settings,
        gateway=request.app.state.llm_gateway,
    )
