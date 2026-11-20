"""Opening and closing browser sessions.

One session is one Playwright MCP server process with one browser, started for
one piece of work and destroyed afterwards. Nothing is pooled or reused, so
cookies and storage cannot pass from one organization, user or run to another.
"""

import asyncio
import contextlib
import logging
import secrets
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

from app.browser.errors import BrowserError, ConnectionFailedError
from app.browser.events import Recorder, SessionEvent
from app.browser.policy import SessionGrant, ToolPolicy
from app.browser.redaction import Redactor
from app.browser.services import EgressClient, LaunchOptions, RunnerClient
from app.browser.session import BrowserSession
from app.core.security.url_safety import DestinationPolicy
from app.mcp.catalog import ToolCatalog
from app.mcp.connection import (
    McpConnection,
    ServerInfo,
    Transport,
    streamable_http_transport,
)
from app.mcp.errors import McpError, McpServerUnavailableError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SessionSpec:
    organization_id: uuid.UUID
    destinations: DestinationPolicy
    grant: SessionGrant
    options: LaunchOptions = field(default_factory=LaunchOptions)
    session_id: uuid.UUID = field(default_factory=uuid.uuid4)


@dataclass(slots=True)
class SessionReport:
    """Filled in while a session runs; complete once it has closed."""

    server_name: str | None = None
    server_version: str | None = None
    discovered: list[dict[str, object]] = field(default_factory=list)
    exposed: list[str] = field(default_factory=list)
    withheld: dict[str, str] = field(default_factory=dict)
    blocked_requests: list[dict[str, object]] = field(default_factory=list)


class BrowserSessionManager:
    def __init__(
        self,
        runner: RunnerClient,
        egress: EgressClient,
        *,
        policy: ToolPolicy | None = None,
        transport_factory: Callable[[str], Transport] = streamable_http_transport,
        connect_attempts: int = 3,
        connect_timeout_s: float = 15.0,
        client_version: str = "0",
    ) -> None:
        self._runner = runner
        self._egress = egress
        self._policy = policy or ToolPolicy()
        self._transport_factory = transport_factory
        self._connect_attempts = connect_attempts
        self._connect_timeout_s = connect_timeout_s
        self._client_version = client_version

    @contextlib.asynccontextmanager
    async def open(
        self, spec: SessionSpec, recorder: Recorder, *, report: SessionReport | None = None
    ) -> AsyncIterator[BrowserSession]:
        """Start a browser for `spec`, yield the session, and always clean up."""
        report = report if report is not None else SessionReport()
        proxy_secret = secrets.token_urlsafe(24)
        session: BrowserSession | None = None
        connection: McpConnection | None = None
        registered = started = False
        await recorder.session(SessionEvent(spec.session_id, "connecting"))
        try:
            await self._egress.register(
                spec.session_id, proxy_secret, spec.destinations, ttl_s=spec.options.ttl_s + 60
            )
            registered = True
            # Created before the request, so that a runner that started the browser but
            # whose answer was lost is still told to stop it.
            started = True
            running = await self._runner.create(
                spec.session_id,
                options=spec.options,
                allowed_origins=sorted(str(o) for o in spec.destinations.allowed_origins),
                proxy_url=self._egress.proxy_url_for(spec.session_id, proxy_secret),
            )
            connection = McpConnection(
                self._transport_factory(running.mcp_url),
                name=str(spec.session_id),
                connect_timeout_s=self._connect_timeout_s,
                call_timeout_s=spec.options.navigation_timeout_ms / 1000 + 15,
                client_version=self._client_version,
            )
            server = await self._connect(connection)
            report.server_name, report.server_version = server.name, server.version
            try:
                catalog = ToolCatalog.build(await connection.list_tools())
            except McpError as exc:
                raise ConnectionFailedError(exc.message, code=exc.code) from None
            exposure = self._policy.exposure(catalog, spec.grant)
            report.discovered = [tool.summary() for tool in catalog.tools()]
            report.exposed = exposure.names()
            report.withheld = {name: d.code for name, d in sorted(exposure.withheld.items())}
            session = BrowserSession(
                session_id=spec.session_id,
                connection=connection,
                catalog=catalog,
                exposure=exposure,
                destinations=spec.destinations,
                recorder=recorder,
                redactor=Redactor([proxy_secret]),
                call_timeout_s=spec.options.navigation_timeout_ms / 1000 + 15,
            )
            await recorder.session(
                SessionEvent(
                    spec.session_id,
                    "connected",
                    {
                        "server": server.name,
                        "server_version": server.version,
                        "protocol_version": server.protocol_version,
                        "tools_discovered": len(catalog),
                        "tools_exposed": len(report.exposed),
                    },
                )
            )
            yield session
        except BrowserError as exc:
            if session is None:
                await recorder.session(
                    SessionEvent(spec.session_id, "failed", {"error_code": exc.code})
                )
            raise
        finally:
            # Runs even when the job is cancelled; each step is independent of the others.
            await asyncio.shield(
                self._cleanup(spec, session, connection, registered, started, report)
            )

    async def _connect(self, connection: McpConnection) -> ServerInfo:
        """The server process may need a moment before it accepts connections."""
        last: McpError | None = None
        for attempt in range(self._connect_attempts):
            if attempt:
                await asyncio.sleep(0.5 * attempt)
            try:
                if attempt:
                    return await connection.reconnect()
                return await connection.connect()
            except McpServerUnavailableError as exc:
                last = exc
            except McpError as exc:
                raise ConnectionFailedError(exc.message, code=exc.code) from None
        assert last is not None  # noqa: S101
        raise ConnectionFailedError(last.message, code=last.code)

    async def _cleanup(
        self,
        spec: SessionSpec,
        session: BrowserSession | None,
        connection: McpConnection | None,
        registered: bool,
        started: bool,
        report: SessionReport,
    ) -> None:
        if session is not None:
            await session.close()
        elif connection is not None:
            await connection.close()
        if started:
            try:
                await self._runner.delete(spec.session_id)
            except BrowserError:
                # The runner also ends sessions itself when their time is up.
                logger.warning(
                    "browser_session_cleanup_failed",
                    extra={"browser_session_id": str(spec.session_id), "step": "runner"},
                )
        if registered:
            try:
                report.blocked_requests = await self._egress.unregister(spec.session_id)
            except BrowserError:
                logger.warning(
                    "browser_session_cleanup_failed",
                    extra={"browser_session_id": str(spec.session_id), "step": "egress"},
                )
