"""Browser checks: open an environment in a real browser, optionally sign in.

This is the first user-facing use of the browser integration. It answers "can
the platform reach this environment and log in with this test account?" before
any test depends on it.

The verdict is computed, not judged: a check passes only if the browser
actually loaded a page inside the environment's allowed origins and, when a
credential was given, every step of its login recipe ran and held.

The API side (`BrowserCheckService`) validates, records and enqueues. The
browser runs in the worker (`BrowserCheckRunner`). A check's state lives in
Redis for an hour; its tool-call trace is written to `agent_execution_logs`.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.browser.errors import BrowserError, LoginFailedError
from app.browser.events import LogRecorder, Recorders, SessionEvent, ToolCallEvent
from app.browser.factory import launch_options
from app.browser.login import LOGIN_ACTIONS, LOGIN_TOOLS, LoginRecipe, run_login
from app.browser.manager import BrowserSessionManager, SessionReport, SessionSpec
from app.browser.policy import SessionGrant, ToolCategory, ToolPolicy
from app.core.config import Settings
from app.core.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationFailedError,
)
from app.core.jobs import JobQueue
from app.core.rate_limit import RateLimiter
from app.core.redis import RedisClient
from app.core.security.crypto import CredentialCipher
from app.core.security.permissions import Permission, has_permission
from app.core.security.url_safety import DestinationPolicy, Origin
from app.db.context import set_current_org
from app.models import AgentExecutionLog, Credential, Environment, McpConnection
from app.repositories.operations import (
    MANAGED_CONNECTION_NAME,
    AgentLogRepository,
    McpConnectionRepository,
)
from app.repositories.projects import (
    ApplicationRepository,
    CredentialRepository,
    EnvironmentRepository,
)
from app.services.audit import AuditEvent, AuditService
from app.services.credentials import CredentialService
from app.services.organizations import OrgContext
from app.services.projects import ProjectContext

logger = logging.getLogger(__name__)

JOB_NAME = "run_browser_check"
_READ_TOOLS = frozenset({"browser_navigate", "browser_snapshot"})
_NODE = "browser_check"


def check_key(organization_id: uuid.UUID, check_id: uuid.UUID) -> str:
    return f"browser_check:{organization_id}:{check_id}"


def thread_id(check_id: uuid.UUID) -> str:
    return f"browser-check:{check_id}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class BrowserStatus:
    configured: bool
    connection: McpConnection | None
    policy: ToolPolicy


class CheckStore:
    """Check state in Redis. Keys include the organization, taken from a verified context."""

    def __init__(self, redis: RedisClient, ttl_s: int) -> None:
        self._redis = redis
        self._ttl_s = ttl_s

    async def save(self, state: dict[str, Any]) -> None:
        key = check_key(uuid.UUID(state["organization_id"]), uuid.UUID(state["id"]))
        await self._redis.set(key, json.dumps(state), ex=self._ttl_s)

    async def load(self, organization_id: uuid.UUID, check_id: uuid.UUID) -> dict[str, Any] | None:
        raw = await self._redis.get(check_key(organization_id, check_id))
        if raw is None:
            return None
        state = json.loads(raw)
        return state if isinstance(state, dict) else None


class BrowserCheckService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        audit: AuditService,
        jobs: JobQueue,
        redis: RedisClient,
        settings: Settings,
    ) -> None:
        self._session = session
        self._audit = audit
        self._jobs = jobs
        self._settings = settings
        self._store = CheckStore(redis, settings.browser_check_result_ttl_s)
        self._limiter = RateLimiter(redis)

    # --- status and administration ---------------------------------------------------

    async def status(self, ctx: OrgContext) -> BrowserStatus:
        ctx.require(Permission.ORG_READ)
        connection = await McpConnectionRepository(self._session, ctx.organization.id).managed()
        return BrowserStatus(self._settings.browser_configured, connection, ToolPolicy())

    async def set_allowed_tools(self, ctx: OrgContext, tools: list[str] | None) -> BrowserStatus:
        """Restrict which tools sessions of this organization may use. None: no restriction.

        This can only narrow what the policy allows; it cannot enable a tool.
        """
        ctx.require(Permission.ENVIRONMENTS_SECURITY)
        policy = ToolPolicy()
        if tools is not None:
            selectable = {
                rule.name
                for rule in policy.rules()
                if not rule.internal and rule.category is not ToolCategory.SENSITIVE
            }
            unknown = sorted(set(tools) - selectable)
            if unknown:
                raise ValidationFailedError(
                    f"Not selectable: {', '.join(unknown)}.", code="tool_not_selectable"
                )
            if not tools:
                raise ValidationFailedError(
                    "Select at least one tool, or remove the restriction.", code="no_tools"
                )
        repository = McpConnectionRepository(self._session, ctx.organization.id)
        connection = await repository.managed(for_update=True)
        if connection is None:
            connection = await repository.add(_new_connection(ctx.organization.id, self._settings))
        connection.allowed_tools = sorted(set(tools)) if tools is not None else []
        await self._session.flush()
        await self._audit.record(
            self._session,
            AuditEvent(
                action="browser.tools.update",
                actor_id=ctx.user.id,
                organization_id=ctx.organization.id,
                resource_type="mcp_connection",
                resource_id=connection.id,
                changes={"allowed_tools": connection.allowed_tools or None},
            ),
        )
        return BrowserStatus(self._settings.browser_configured, connection, policy)

    # --- checks ----------------------------------------------------------------------

    async def start(
        self, ctx: ProjectContext, environment: Environment, *, credential_id: uuid.UUID | None
    ) -> dict[str, Any]:
        ctx.require(Permission.BROWSER_RUN)
        ctx.require_writable()
        if not self._settings.browser_configured:
            raise ServiceUnavailableError(
                "Browser automation is not configured on this installation.",
                code="browser_not_configured",
            )
        if environment.status != "active":
            raise ConflictError("This environment is not active.", code="environment_inactive")
        if "navigate" not in environment.allowed_actions:
            raise ConflictError(
                "This environment does not allow navigation.", code="action_not_allowed"
            )
        approved = False
        if credential_id is not None:
            credential = await CredentialRepository(
                self._session, ctx.organization_id
            ).get_in_environment(environment.id, credential_id)
            if credential is None:
                raise NotFoundError("Credential not found.")
            approved = self._require_login_allowed(ctx, environment, credential)
        await self._limiter.enforce(
            "browser-check",
            str(ctx.organization_id),
            limit=self._settings.browser_check_limit,
            window_s=self._settings.browser_check_window_s,
            message="Too many browser checks. Please try again later.",
        )
        check_id = uuid.uuid4()
        state: dict[str, Any] = {
            "id": str(check_id),
            "organization_id": str(ctx.organization_id),
            "project_id": str(ctx.project.id),
            "environment_id": str(environment.id),
            "credential_id": str(credential_id) if credential_id else None,
            "requested_by": str(ctx.user.id),
            "status": "queued",
            "created_at": _now(),
            "finished_at": None,
            "result": None,
            "error": None,
        }
        await self._store.save(state)
        await self._audit.record(
            self._session,
            AuditEvent(
                action="browser_check.start",
                actor_id=ctx.user.id,
                organization_id=ctx.organization_id,
                resource_type="environment",
                resource_id=environment.id,
                changes={
                    "check_id": str(check_id),
                    "credential_id": state["credential_id"],
                    "state_changes_approved": approved,
                },
            ),
        )
        await self._jobs.enqueue(
            JOB_NAME,
            organization_id=str(ctx.organization_id),
            check_id=str(check_id),
            state_changes_approved=approved,
        )
        return state

    @staticmethod
    def _require_login_allowed(
        ctx: ProjectContext, environment: Environment, credential: Credential
    ) -> bool:
        """Signing in types and clicks, so it is a state-changing use of the environment."""
        if credential.source != "stored":
            raise ConflictError(
                "Only credentials stored on the platform can be used for a login check.",
                code="credential_not_stored",
            )
        try:
            LoginRecipe.model_validate(list(credential.login_recipe))
        except ValidationError:
            raise ConflictError(
                "This credential has no usable login recipe. Edit the credential and "
                "describe the login steps.",
                code="login_recipe_missing",
            ) from None
        if environment.is_read_only:
            raise ConflictError(
                "This environment is read-only, so the platform may not sign in to it.",
                code="environment_read_only",
            )
        if not set(environment.allowed_actions) >= LOGIN_ACTIONS:
            raise ConflictError(
                "Signing in needs the navigate, read, type and click actions on this environment.",
                code="action_not_allowed",
            )
        if not environment.requires_approval:
            return False
        if not has_permission(ctx.org.role, Permission.ENVIRONMENTS_SECURITY):
            raise ForbiddenError(
                "This environment requires approval for actions that change data. "
                "Ask an organization admin to run the login check.",
                code="approval_required",
            )
        # An admin asking for it is the approval; it is recorded in the audit log.
        return True

    async def get(
        self, ctx: ProjectContext, environment: Environment, check_id: uuid.UUID
    ) -> tuple[dict[str, Any], Sequence[AgentExecutionLog]]:
        ctx.require(Permission.BROWSER_RUN)
        state = await self._store.load(ctx.organization_id, check_id)
        if (
            state is None
            or state.get("project_id") != str(ctx.project.id)
            or state.get("environment_id") != str(environment.id)
        ):
            raise NotFoundError("Browser check not found. Results are kept for a limited time.")
        calls = await AgentLogRepository(self._session, ctx.organization_id).for_thread(
            thread_id(check_id)
        )
        return state, calls


def _new_connection(organization_id: uuid.UUID, settings: Settings) -> McpConnection:
    return McpConnection(
        organization_id=organization_id,
        name=MANAGED_CONNECTION_NAME,
        mode="managed",
        transport="streamable_http",
        browser=settings.browser_name,
        status="active",
    )


# --- worker side -------------------------------------------------------------------------


class DbRecorder:
    """Writes the trace to `agent_execution_logs`, one short transaction per event."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        organization_id: uuid.UUID,
        thread: str,
        node: str,
    ) -> None:
        self._session_factory = session_factory
        self._organization_id = organization_id
        self._thread = thread
        self._node = node

    async def _write(self, row: AgentExecutionLog) -> None:
        async with self._session_factory() as session, session.begin():
            await set_current_org(session, self._organization_id)
            session.add(row)

    async def tool_call(self, event: ToolCallEvent) -> None:
        error = None
        if event.error_code is not None:
            error = {"code": event.error_code, "message": event.error_message}
        await self._write(
            AgentExecutionLog(
                organization_id=self._organization_id,
                thread_id=self._thread,
                node=self._node,
                event_type="tool_call",
                tool_name=event.tool,
                tool_args={"category": event.category, "arguments": event.arguments},
                tool_result_summary=event.result_summary,
                latency_ms=event.duration_ms,
                status=event.status,
                error=error,
            )
        )

    async def session(self, event: SessionEvent) -> None:
        if event.kind != "failed":
            return
        await self._write(
            AgentExecutionLog(
                organization_id=self._organization_id,
                thread_id=self._thread,
                node=self._node,
                event_type="error",
                status="failed",
                error={"code": event.detail.get("error_code", "session_failed")},
            )
        )


@dataclass(slots=True)
class _Plan:
    environment_id: uuid.UUID
    base_url: str
    destinations: DestinationPolicy
    grant: SessionGrant
    viewport: dict[str, Any] | None
    recipe: LoginRecipe | None
    fields: dict[str, str]


class BrowserCheckRunner:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        redis: RedisClient,
        cipher: CredentialCipher,
        manager: BrowserSessionManager | None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._store = CheckStore(redis, settings.browser_check_result_ttl_s)
        self._cipher = cipher
        self._manager = manager

    async def run(
        self, *, organization_id: uuid.UUID, check_id: uuid.UUID, state_changes_approved: bool
    ) -> str:
        state = await self._store.load(organization_id, check_id)
        if state is None or state.get("status") != "queued":
            return "discarded"
        state["status"] = "running"
        await self._store.save(state)

        report = SessionReport()
        result: dict[str, Any] | None = None
        error: dict[str, str] | None = None
        try:
            if self._manager is None:
                raise BrowserError(
                    "Browser automation is not configured.", code="browser_not_configured"
                )
            plan = await self._plan(organization_id, check_id, state, state_changes_approved)
            async with asyncio.timeout(self._settings.browser_check_timeout_s):
                result = await self._browse(organization_id, check_id, plan, report)
        except BrowserError as exc:
            error = {"code": exc.code, "message": exc.message}
        except TimeoutError:
            error = {"code": "browser_timeout", "message": "The check took too long."}
        except Exception:
            logger.exception("browser_check_failed", extra={"check_id": str(check_id)})
            error = {"code": "internal_error", "message": "The check failed unexpectedly."}

        # "passed" is only ever set here, and only when the browser work completed.
        state["status"] = "passed" if result is not None and error is None else "failed"
        state["result"] = result
        state["error"] = error
        state["finished_at"] = _now()
        state["session"] = {
            "server": report.server_name,
            "server_version": report.server_version,
            "tools_exposed": report.exposed,
            "tools_withheld": report.withheld,
            "blocked_requests": report.blocked_requests,
        }
        await self._store.save(state)
        await self._remember_connection(organization_id, report, error)
        return str(state["status"])

    async def _plan(
        self,
        organization_id: uuid.UUID,
        check_id: uuid.UUID,
        state: dict[str, Any],
        state_changes_approved: bool,
    ) -> _Plan:
        """Read everything the browser work needs, in one transaction, then let go of it."""
        async with self._session_factory() as session, session.begin():
            await set_current_org(session, organization_id)
            environments = EnvironmentRepository(session, organization_id)
            environment = await environments.get(uuid.UUID(state["environment_id"]))
            if environment is None or environment.status != "active":
                raise BrowserError("The environment no longer exists.", code="environment_gone")
            application = await ApplicationRepository(session, organization_id).get(
                environment.application_id
            )
            if application is None or str(application.project_id) != state["project_id"]:
                raise BrowserError("The environment no longer exists.", code="environment_gone")
            origins = (await environments.origins([environment.id]))[environment.id]
            destinations = DestinationPolicy(
                allowed_origins=frozenset(Origin(o.scheme, o.host, o.port) for o in origins),
                allow_private_network=environment.allow_private_network,
                allow_http=environment.kind != "production",
            )
            connection = await McpConnectionRepository(session, organization_id).managed()
            restricted = (
                frozenset(connection.allowed_tools)
                if connection and connection.allowed_tools
                else None
            )
            recipe: LoginRecipe | None = None
            fields: dict[str, str] = {}
            if state.get("credential_id"):
                credential = await CredentialRepository(
                    session, organization_id
                ).get_in_environment(environment.id, uuid.UUID(state["credential_id"]))
                if credential is None:
                    raise BrowserError("The credential no longer exists.", code="credential_gone")
                try:
                    recipe = LoginRecipe.model_validate(list(credential.login_recipe))
                except ValidationError:
                    raise LoginFailedError(
                        "The credential has no usable login recipe.", code="login_recipe_missing"
                    ) from None
                fields = await CredentialService(
                    session=session,
                    audit=AuditService(self._session_factory),
                    cipher=self._cipher,
                ).reveal_for_job(
                    credential,
                    requested_by=uuid.UUID(state["requested_by"]),
                    purpose="browser_check",
                    reference_id=check_id,
                )
            grant = SessionGrant(
                allowed_actions=frozenset(environment.allowed_actions),
                read_only=environment.is_read_only,
                requires_approval=environment.requires_approval,
                state_changes_approved=state_changes_approved,
                requested_tools=LOGIN_TOOLS if recipe is not None else _READ_TOOLS,
                organization_tools=restricted,
            )
            return _Plan(
                environment_id=environment.id,
                base_url=environment.base_url,
                destinations=destinations,
                grant=grant,
                viewport=environment.default_viewport,
                recipe=recipe,
                fields=fields,
            )

    async def _browse(
        self, organization_id: uuid.UUID, check_id: uuid.UUID, plan: _Plan, report: SessionReport
    ) -> dict[str, Any]:
        assert self._manager is not None  # noqa: S101 - checked by the caller
        recorder = Recorders(
            LogRecorder(),
            DbRecorder(
                self._session_factory,
                organization_id=organization_id,
                thread=thread_id(check_id),
                node=_NODE,
            ),
        )
        spec = SessionSpec(
            organization_id=organization_id,
            destinations=plan.destinations,
            grant=plan.grant,
            options=launch_options(self._settings, plan.viewport),
        )
        async with self._manager.open(spec, recorder, report=report) as browser:
            opened = await browser.call("browser_navigate", {"url": plan.base_url})
            if opened.page_url is None:
                # The server did not say where the browser is, so nothing is known.
                raise BrowserError(
                    "The browser did not report which page it loaded.", code="page_unknown"
                )
            result: dict[str, Any] = {
                "page_url": opened.page_url,
                "page_title": opened.page_title,
                "logged_in": None,
                "login_steps_performed": None,
            }
            if plan.recipe is not None:
                login = await run_login(
                    browser, base_url=plan.base_url, recipe=plan.recipe, fields=plan.fields
                )
                result.update(
                    logged_in=True,
                    login_steps_performed=login.steps_performed,
                    page_url=login.page_url or result["page_url"],
                    page_title=login.page_title or result["page_title"],
                )
            result["actions_performed"] = asdict(browser.stats)["performed"]
            return result

    async def _remember_connection(
        self, organization_id: uuid.UUID, report: SessionReport, error: dict[str, str] | None
    ) -> None:
        """Keep the latest connection facts on the organization's managed connection."""
        if self._manager is None:
            return
        reached = report.server_version is not None
        try:
            async with self._session_factory() as session, session.begin():
                await set_current_org(session, organization_id)
                repository = McpConnectionRepository(session, organization_id)
                connection = await repository.managed(for_update=True)
                if connection is None:
                    connection = await repository.add(
                        _new_connection(organization_id, self._settings)
                    )
                connection.last_health_check_at = datetime.now(UTC)
                if reached:
                    connection.server_version = f"{report.server_name} {report.server_version}"
                if error is not None and error["code"] in _CONNECTION_ERRORS:
                    connection.status = "unhealthy"
                    connection.last_error = f"{error['code']}: {error['message']}"[:500]
                elif reached and report.discovered:
                    connection.status = "active"
                    connection.discovered_tools = report.discovered
                    connection.last_error = None
        except Exception:
            logger.exception("mcp_connection_update_failed")


_CONNECTION_ERRORS = frozenset(
    {
        "runner_unavailable",
        "runner_busy",
        "mcp_server_unavailable",
        "mcp_initialization_failed",
        "mcp_discovery_failed",
        "mcp_connection_failed",
        "browser_launch_failed",
    }
)
