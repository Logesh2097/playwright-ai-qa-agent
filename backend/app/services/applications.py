"""Applications under test and their environments."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import (
    ConflictError,
    ErrorDetail,
    ForbiddenError,
    NotFoundError,
    ValidationFailedError,
)
from app.core.rate_limit import RateLimiter
from app.core.redis import RedisClient
from app.core.security.permissions import Permission, has_permission
from app.core.security.url_safety import (
    AccessValidator,
    DestinationPolicy,
    Origin,
    UrlNotAllowedError,
    host_in_domains,
    normalize_domain,
    parse_origin,
)
from app.db.errors import is_unique_violation
from app.models import Application, Environment, EnvironmentAllowedOrigin
from app.models.project import BROWSER_ACTIONS, READ_ONLY_ACTIONS
from app.repositories.projects import (
    ApplicationRepository,
    CredentialRepository,
    EnvironmentRepository,
)
from app.schemas.projects import (
    ApplicationCreate,
    ApplicationUpdate,
    EnvironmentAuthConfig,
    EnvironmentCreate,
    EnvironmentUpdate,
    EnvironmentVariable,
)
from app.services.access_validation import AccessProbe, ProbeResult
from app.services.audit import AuditEvent, AuditService
from app.services.projects import ProjectContext
from app.utils.redaction import is_sensitive_key

_SECURITY_MESSAGE = "Only organization admins can configure this."


def invalid(field: str, exc: UrlNotAllowedError) -> ValidationFailedError:
    return ValidationFailedError(
        exc.message,
        code=exc.code,
        errors=[ErrorDetail(field=field, code=exc.code, message=exc.message)],
    )


@dataclass(frozen=True, slots=True)
class EnvironmentView:
    environment: Environment
    origins: Sequence[EnvironmentAllowedOrigin]
    credential_count: int


@dataclass(frozen=True, slots=True)
class _OriginSpec:
    origin: Origin
    purpose: str
    field: str


class ApplicationService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        audit: AuditService,
        validator: AccessValidator,
        probe: AccessProbe,
        redis: RedisClient,
        settings: Settings,
    ) -> None:
        self._session = session
        self._audit = audit
        self._validator = validator
        self._probe = probe
        self._limiter = RateLimiter(redis)
        self._settings = settings

    # --- applications ------------------------------------------------------------

    async def list_applications(self, ctx: ProjectContext) -> list[tuple[Application, int]]:
        repository = ApplicationRepository(self._session, ctx.organization_id)
        applications = await repository.list_in_project(ctx.project.id)
        counts = await repository.environment_counts([a.id for a in applications])
        return [(application, counts.get(application.id, 0)) for application in applications]

    async def get_application(self, ctx: ProjectContext, application_id: uuid.UUID) -> Application:
        application = await ApplicationRepository(
            self._session, ctx.organization_id
        ).get_in_project(ctx.project.id, application_id)
        if application is None:
            raise NotFoundError("Application not found.")
        return application

    async def environment_count(self, ctx: ProjectContext, application: Application) -> int:
        counts = await ApplicationRepository(self._session, ctx.organization_id).environment_counts(
            [application.id]
        )
        return counts.get(application.id, 0)

    async def create_application(self, ctx: ProjectContext, data: ApplicationCreate) -> Application:
        ctx.require(Permission.APPLICATIONS_MANAGE)
        ctx.require_writable()
        base_origin, domains = self._application_urls(data.base_url, data.allowed_domains)
        application = Application(
            organization_id=ctx.organization_id,
            project_id=ctx.project.id,
            name=data.name,
            description=data.description,
            base_url=data.base_url,
            type=data.type,
            framework=data.framework,
            auth_method=data.auth_method,
            default_browser_config=data.default_browser_config.model_dump(exclude_none=True),
            allowed_domains=domains,
            created_by=ctx.user.id,
        )
        await self._add(application, "An application with this name already exists in the project.")
        await self._record(
            ctx,
            "application.create",
            application.id,
            {"name": data.name, "base_url": str(base_origin), "allowed_domains": domains},
        )
        return application

    async def update_application(
        self, ctx: ProjectContext, application: Application, data: ApplicationUpdate
    ) -> Application:
        ctx.require(Permission.APPLICATIONS_MANAGE)
        ctx.require_writable()
        fields = data.model_fields_set
        changes: dict[str, Any] = {}

        base_url = data.base_url if data.base_url is not None else application.base_url
        requested = (
            data.allowed_domains
            if data.allowed_domains is not None
            else application.allowed_domains
        )
        if "base_url" in fields or "allowed_domains" in fields:
            _, domains = self._application_urls(base_url, requested)
            if domains != application.allowed_domains:
                await self._require_environments_within(ctx, application, domains)
                changes["allowed_domains"] = [application.allowed_domains, domains]
                application.allowed_domains = domains
            if base_url != application.base_url:
                changes["base_url"] = [application.base_url, base_url]
                application.base_url = base_url

        for name in ("name", "type", "auth_method", "status"):
            value = getattr(data, name)
            if value is not None and value != getattr(application, name):
                changes[name] = [getattr(application, name), value]
                setattr(application, name, value)
        for name in ("description", "framework"):
            if name in fields and getattr(data, name) != getattr(application, name):
                changes[name] = "changed"
                setattr(application, name, getattr(data, name))
        if data.default_browser_config is not None:
            config = data.default_browser_config.model_dump(exclude_none=True)
            if config != application.default_browser_config:
                changes["default_browser_config"] = "changed"
                application.default_browser_config = config

        if changes:
            await self._flush("An application with this name already exists in the project.")
            await self._record(ctx, "application.update", application.id, changes)
        return application

    async def delete_application(self, ctx: ProjectContext, application: Application) -> None:
        ctx.require(Permission.APPLICATIONS_MANAGE)
        ctx.require_writable()
        environments = EnvironmentRepository(self._session, ctx.organization_id)
        existing = await environments.list_in_application(application.id)
        if any(e.kind == "production" for e in existing):
            ctx.org.require(Permission.ENVIRONMENTS_SECURITY)
        now = datetime.now(UTC)
        credentials = CredentialRepository(self._session, ctx.organization_id)
        for environment in existing:
            await credentials.delete_for_environment(environment.id)
            environment.deleted_at = now
        application.deleted_at = now
        await self._session.flush()
        await self._record(ctx, "application.delete", application.id, {"name": application.name})

    # --- environments ------------------------------------------------------------

    async def list_environments(
        self, ctx: ProjectContext, application: Application
    ) -> list[EnvironmentView]:
        repository = EnvironmentRepository(self._session, ctx.organization_id)
        environments = await repository.list_in_application(application.id)
        ids = [environment.id for environment in environments]
        origins = await repository.origins(ids)
        counts = await repository.credential_counts(ids)
        return [
            EnvironmentView(environment, origins[environment.id], counts.get(environment.id, 0))
            for environment in environments
        ]

    async def get_environment(
        self, ctx: ProjectContext, application: Application, environment_id: uuid.UUID
    ) -> Environment:
        environment = await EnvironmentRepository(
            self._session, ctx.organization_id
        ).get_in_application(application.id, environment_id)
        if environment is None:
            raise NotFoundError("Environment not found.")
        return environment

    async def view_environment(
        self, ctx: ProjectContext, environment: Environment
    ) -> EnvironmentView:
        repository = EnvironmentRepository(self._session, ctx.organization_id)
        origins = await repository.origins([environment.id])
        counts = await repository.credential_counts([environment.id])
        return EnvironmentView(environment, origins[environment.id], counts.get(environment.id, 0))

    async def create_environment(
        self, ctx: ProjectContext, application: Application, data: EnvironmentCreate
    ) -> Environment:
        ctx.require(Permission.ENVIRONMENTS_MANAGE)
        ctx.require_writable()
        production = data.kind == "production"
        is_read_only = production if data.is_read_only is None else data.is_read_only
        requires_approval = production if data.requires_approval is None else data.requires_approval
        if production or data.allow_private_network:
            self._require_security(ctx)

        specs = await self._validated_origins(
            application,
            kind=data.kind,
            base_url=data.base_url,
            api_base_url=data.api_base_url,
            additional=[(o.url, o.purpose) for o in data.additional_origins],
            allow_private=data.allow_private_network,
        )
        origins = {spec.origin for spec in specs}
        environment = Environment(
            organization_id=ctx.organization_id,
            application_id=application.id,
            name=data.name,
            kind=data.kind,
            base_url=data.base_url,
            api_base_url=data.api_base_url,
            is_read_only=is_read_only,
            requires_approval=requires_approval,
            allow_private_network=data.allow_private_network,
            variables=self._variables(data.variables, credential_names=set()),
            auth_config=self._auth_config(data.auth_config, origins, data.kind, set()),
            allowed_actions=self._actions(data.allowed_actions, is_read_only),
            created_by=ctx.user.id,
        )
        await self._add(environment, "An environment with this name already exists.")
        await self._store_origins(ctx, environment, specs)
        await self._record(
            ctx,
            "environment.create",
            environment.id,
            {
                "name": data.name,
                "kind": data.kind,
                "allowed_origins": sorted(str(o) for o in origins),
                "allow_private_network": data.allow_private_network,
            },
            resource_type="environment",
        )
        return environment

    async def update_environment(
        self,
        ctx: ProjectContext,
        application: Application,
        environment: Environment,
        data: EnvironmentUpdate,
    ) -> Environment:
        ctx.require(Permission.ENVIRONMENTS_MANAGE)
        ctx.require_writable()
        fields = data.model_fields_set
        repository = EnvironmentRepository(self._session, ctx.organization_id)
        current_origins = (await repository.origins([environment.id]))[environment.id]

        kind = data.kind or environment.kind
        allow_private = (
            environment.allow_private_network
            if data.allow_private_network is None
            else data.allow_private_network
        )
        is_read_only = environment.is_read_only if data.is_read_only is None else data.is_read_only
        requires_approval = (
            environment.requires_approval
            if data.requires_approval is None
            else data.requires_approval
        )
        # Touching a production environment, turning something into one, or widening
        # network access is reserved for admins.
        if (
            environment.kind == "production"
            or kind == "production"
            or (allow_private and not environment.allow_private_network)
        ):
            self._require_security(ctx)

        changes: dict[str, Any] = {}
        url_fields = {
            "kind",
            "base_url",
            "api_base_url",
            "additional_origins",
            "allow_private_network",
        }
        base_url = data.base_url or environment.base_url
        api_base_url = data.api_base_url if "api_base_url" in fields else environment.api_base_url
        if fields & url_fields:
            additional = (
                [(o.url, o.purpose) for o in data.additional_origins]
                if data.additional_origins is not None
                else [
                    (str(Origin(o.scheme, o.host, o.port)), o.purpose)
                    for o in current_origins
                    if o.purpose not in ("app", "api")
                ]
            )
            specs = await self._validated_origins(
                application,
                kind=kind,
                base_url=base_url,
                api_base_url=api_base_url,
                additional=additional,
                allow_private=allow_private,
            )
            before = sorted(str(Origin(o.scheme, o.host, o.port)) for o in current_origins)
            after = sorted({str(spec.origin) for spec in specs})
            if before != after:
                changes["allowed_origins"] = [before, after]
            await self._store_origins(ctx, environment, specs)
            origins = {spec.origin for spec in specs}
        else:
            origins = {Origin(o.scheme, o.host, o.port) for o in current_origins}

        updates: dict[str, Any] = {
            "name": data.name or environment.name,
            "kind": kind,
            "base_url": base_url,
            "api_base_url": api_base_url,
            "is_read_only": is_read_only,
            "requires_approval": requires_approval,
            "allow_private_network": allow_private,
            "status": data.status or environment.status,
        }
        for name, value in updates.items():
            if value != getattr(environment, name):
                changes[name] = [getattr(environment, name), value]
                setattr(environment, name, value)

        credential_names = await CredentialRepository(
            self._session, ctx.organization_id
        ).names_in_environment(environment.id)
        if data.variables is not None:
            variables = self._variables(data.variables, credential_names=credential_names)
            if variables != environment.variables:
                changes["variables"] = "changed"
                environment.variables = variables
        if data.auth_config is not None or fields & url_fields:
            config = data.auth_config or EnvironmentAuthConfig.model_validate(
                environment.auth_config
            )
            auth_config = self._auth_config(config, origins, kind, credential_names)
            if auth_config != environment.auth_config:
                changes["auth_config"] = "changed"
                environment.auth_config = auth_config
        if data.allowed_actions is not None or "is_read_only" in fields:
            requested = (
                data.allowed_actions
                if data.allowed_actions is not None
                else (None if is_read_only else list(environment.allowed_actions))
            )
            actions = self._actions(requested, is_read_only)
            if actions != environment.allowed_actions:
                changes["allowed_actions"] = [environment.allowed_actions, actions]
                environment.allowed_actions = actions

        if changes:
            await self._flush("An environment with this name already exists.")
            await self._record(
                ctx, "environment.update", environment.id, changes, resource_type="environment"
            )
        return environment

    async def delete_environment(self, ctx: ProjectContext, environment: Environment) -> None:
        ctx.require(Permission.ENVIRONMENTS_MANAGE)
        ctx.require_writable()
        if environment.kind == "production":
            self._require_security(ctx)
        await CredentialRepository(self._session, ctx.organization_id).delete_for_environment(
            environment.id
        )
        environment.deleted_at = datetime.now(UTC)
        await self._session.flush()
        await self._record(
            ctx,
            "environment.delete",
            environment.id,
            {"name": environment.name},
            resource_type="environment",
        )

    async def policy_for(self, ctx: ProjectContext, environment: Environment) -> DestinationPolicy:
        """What the agent, the test engine or a probe may reach for this environment."""
        stored = (
            await EnvironmentRepository(self._session, ctx.organization_id).origins(
                [environment.id]
            )
        )[environment.id]
        return DestinationPolicy(
            allowed_origins=frozenset(Origin(o.scheme, o.host, o.port) for o in stored),
            allow_private_network=environment.allow_private_network,
            allow_http=environment.kind != "production",
        )

    async def check_access(self, ctx: ProjectContext, environment: Environment) -> ProbeResult:
        """Contact the environment's base URL, validating every hop along the way."""
        ctx.require(Permission.ENVIRONMENTS_MANAGE)
        await self._limiter.enforce(
            "access-check",
            str(ctx.organization_id),
            limit=self._settings.access_probe_limit,
            window_s=self._settings.access_probe_window_s,
        )
        result = await self._probe.probe(
            environment.base_url, await self.policy_for(ctx, environment)
        )
        await self._record(
            ctx,
            "environment.access_check",
            environment.id,
            {"reachable": result.reachable, "error_code": result.error_code},
            resource_type="environment",
        )
        return result

    # --- validation helpers ----------------------------------------------------------

    @staticmethod
    def _application_urls(
        base_url: str, requested_domains: Sequence[str]
    ) -> tuple[Origin, list[str]]:
        try:
            origin = parse_origin(base_url)
        except UrlNotAllowedError as exc:
            raise invalid("base_url", exc) from exc
        domains: list[str] = []
        for index, entry in enumerate(requested_domains):
            try:
                domain = normalize_domain(entry)
            except UrlNotAllowedError as exc:
                raise invalid(f"allowed_domains.{index}", exc) from exc
            if domain not in domains:
                domains.append(domain)
        if not domains:
            domains = [origin.host]
        if not host_in_domains(origin.host, domains):
            raise ValidationFailedError(
                f"{origin.host} is not covered by the allowed domains.",
                code="domain_not_allowed",
                errors=[
                    ErrorDetail(
                        field="base_url",
                        code="domain_not_allowed",
                        message="host is outside the allowed domains",
                    )
                ],
            )
        return origin, sorted(domains)

    async def _require_environments_within(
        self, ctx: ProjectContext, application: Application, domains: list[str]
    ) -> None:
        """Narrowing the domain list must not strand an existing environment."""
        for view in await self.list_environments(ctx, application):
            for origin in view.origins:
                if not host_in_domains(origin.host, domains):
                    raise ValidationFailedError(
                        f"Environment '{view.environment.name}' uses {origin.host}, "
                        "which the new list would no longer cover.",
                        code="domain_in_use",
                    )

    async def _validated_origins(
        self,
        application: Application,
        *,
        kind: str,
        base_url: str,
        api_base_url: str | None,
        additional: Sequence[tuple[str, str]],
        allow_private: bool,
    ) -> list[_OriginSpec]:
        """Syntax, application domain list, then DNS and address checks for every origin."""
        allow_http = kind != "production"
        candidates: list[tuple[str, str, str]] = [(base_url, "app", "base_url")]
        if api_base_url:
            candidates.append((api_base_url, "api", "api_base_url"))
        candidates.extend(
            (url, purpose, f"additional_origins.{index}.url")
            for index, (url, purpose) in enumerate(additional)
        )
        specs: dict[Origin, _OriginSpec] = {}
        for url, purpose, field in candidates:
            try:
                origin = parse_origin(url, allow_http=allow_http)
                if not host_in_domains(origin.host, application.allowed_domains):
                    raise UrlNotAllowedError(
                        "domain_not_allowed",
                        f"{origin.host} is not in the application's allowed domains.",
                    )
                await self._validator.resolve_public(origin, allow_private=allow_private)
            except UrlNotAllowedError as exc:
                raise invalid(field, exc) from exc
            specs.setdefault(origin, _OriginSpec(origin=origin, purpose=purpose, field=field))
        return list(specs.values())

    async def _store_origins(
        self, ctx: ProjectContext, environment: Environment, specs: Sequence[_OriginSpec]
    ) -> None:
        await EnvironmentRepository(self._session, ctx.organization_id).replace_origins(
            environment.id,
            [
                EnvironmentAllowedOrigin(
                    organization_id=ctx.organization_id,
                    environment_id=environment.id,
                    scheme=spec.origin.scheme,
                    host=spec.origin.host,
                    port=spec.origin.port,
                    purpose=spec.purpose,
                )
                for spec in specs
            ],
        )

    @staticmethod
    def _variables(
        variables: Sequence[EnvironmentVariable], *, credential_names: set[str]
    ) -> list[dict[str, str]]:
        seen: set[str] = set()
        stored: list[dict[str, str]] = []
        for index, variable in enumerate(variables):
            field = f"variables.{index}"
            if variable.name in seen:
                raise ValidationFailedError(
                    f"Variable {variable.name} is listed twice.",
                    code="duplicate_variable",
                    errors=[ErrorDetail(field=field, code="duplicate", message="duplicate name")],
                )
            seen.add(variable.name)
            if variable.secret_ref is not None:
                if variable.secret_ref not in credential_names:
                    raise ValidationFailedError(
                        f"No credential named '{variable.secret_ref}' exists in this environment.",
                        code="unknown_credential",
                        errors=[
                            ErrorDetail(field=field, code="unknown_credential", message="not found")
                        ],
                    )
                stored.append({"name": variable.name, "secret_ref": variable.secret_ref})
            else:
                # Plain values are stored and shown as they are, so anything that
                # sounds like a secret has to go through a credential instead.
                if is_sensitive_key(variable.name):
                    raise ValidationFailedError(
                        f"{variable.name} looks like a secret. Save it as a credential and "
                        "reference it with secret_ref.",
                        code="secret_in_variable",
                        errors=[
                            ErrorDetail(
                                field=field, code="secret_in_variable", message="use secret_ref"
                            )
                        ],
                    )
                stored.append({"name": variable.name, "value": variable.value or ""})
        return stored

    @staticmethod
    def _auth_config(
        config: EnvironmentAuthConfig, origins: set[Origin], kind: str, credential_names: set[str]
    ) -> dict[str, Any]:
        if config.login_url is not None:
            try:
                origin = parse_origin(config.login_url, allow_http=kind != "production")
            except UrlNotAllowedError as exc:
                raise invalid("auth_config.login_url", exc) from exc
            if origin not in origins:
                raise ValidationFailedError(
                    f"{origin} is not one of this environment's allowed origins.",
                    code="origin_not_allowlisted",
                    errors=[
                        ErrorDetail(
                            field="auth_config.login_url",
                            code="origin_not_allowlisted",
                            message="not an allowed origin",
                        )
                    ],
                )
        if (
            config.default_credential is not None
            and config.default_credential not in credential_names
        ):
            raise ValidationFailedError(
                f"No credential named '{config.default_credential}' exists in this environment.",
                code="unknown_credential",
            )
        return config.model_dump(exclude_none=True)

    @staticmethod
    def _actions(requested: Sequence[str] | None, is_read_only: bool) -> list[str]:
        if requested is None:
            requested = READ_ONLY_ACTIONS if is_read_only else BROWSER_ACTIONS
        actions = [action for action in BROWSER_ACTIONS if action in set(requested)]
        if is_read_only and not set(actions) <= set(READ_ONLY_ACTIONS):
            raise ValidationFailedError(
                "A read-only environment only permits navigate, read and api_read.",
                code="actions_conflict_read_only",
            )
        if "navigate" not in actions:
            raise ValidationFailedError(
                "The 'navigate' action is required.", code="navigate_required"
            )
        return actions

    @staticmethod
    def _require_security(ctx: ProjectContext) -> None:
        if not has_permission(ctx.org.role, Permission.ENVIRONMENTS_SECURITY):
            raise ForbiddenError(_SECURITY_MESSAGE, code="admin_required")

    # --- persistence helpers ---------------------------------------------------------

    async def _add(self, entity: Application | Environment, conflict_message: str) -> None:
        self._session.add(entity)
        await self._flush(conflict_message)

    async def _flush(self, conflict_message: str) -> None:
        try:
            await self._session.flush()
        except IntegrityError as exc:
            if is_unique_violation(exc):
                raise ConflictError(conflict_message, code="name_taken") from exc
            raise

    async def _record(
        self,
        ctx: ProjectContext,
        action: str,
        resource_id: uuid.UUID,
        changes: dict[str, Any],
        *,
        resource_type: str = "application",
    ) -> None:
        await self._audit.record(
            self._session,
            AuditEvent(
                action=action,
                actor_id=ctx.user.id,
                organization_id=ctx.organization_id,
                resource_type=resource_type,
                resource_id=resource_id,
                changes=changes,
            ),
        )
