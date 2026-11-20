"""One connection to one MCP server, on the official Python SDK.

The SDK's transport and session are context managers that must be entered and
left in the same task, and a broken transport surfaces by cancelling whatever
is inside them. So each connection has one owner task that holds both contexts
and performs requests handed to it; callers only await a result. A failure of
the transport then reaches callers as `McpConnectionLostError` instead of as a
cancellation or an exception group.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Literal

from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from app.mcp.catalog import ToolSpec
from app.mcp.errors import (
    McpConnectionLostError,
    McpDiscoveryError,
    McpError,
    McpInitializationError,
    McpNotConnectedError,
    McpProtocolError,
    McpServerUnavailableError,
    McpTimeoutError,
)
from app.mcp.results import RawToolResult, parse_tool_result

logger = logging.getLogger(__name__)

# Yields the SDK's (read stream, write stream, ...) for one connection.
Transport = Callable[[], AbstractAsyncContextManager[Any]]
ConnectionStatus = Literal["disconnected", "connecting", "connected", "failed", "closed"]

_REQUEST_TIMEOUT_CODE = -32001
_MAX_TOOL_PAGES = 20
# How long past its own deadline a request may take before the connection is given up on.
_STUCK_GRACE_S = 5.0


def streamable_http_transport(url: str) -> Transport:
    """Streamable HTTP, the transport Playwright MCP serves at `/mcp`."""
    return lambda: streamable_http_client(url)


@dataclass(frozen=True, slots=True)
class ServerInfo:
    name: str
    version: str
    protocol_version: str
    capabilities: tuple[str, ...]


@dataclass(slots=True)
class _Job:
    run: Callable[[ClientSession], Awaitable[Any]]
    future: "asyncio.Future[Any]"


class McpConnection:
    def __init__(
        self,
        transport: Transport,
        *,
        name: str = "mcp",
        connect_timeout_s: float = 15.0,
        call_timeout_s: float = 60.0,
        client_name: str = "qa-agent",
        client_version: str = "0",
    ) -> None:
        self._transport = transport
        self._name = name
        self._connect_timeout_s = connect_timeout_s
        self._call_timeout_s = call_timeout_s
        self._client_info = types.Implementation(name=client_name, version=client_version)
        self._status: ConnectionStatus = "disconnected"
        self._server: ServerInfo | None = None
        self._owner: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[_Job | None] = asyncio.Queue()
        self._ready: asyncio.Future[None] | None = None
        self._closing = False
        self._failure: McpError | None = None

    # --- state -----------------------------------------------------------------------

    @property
    def status(self) -> ConnectionStatus:
        return self._status

    @property
    def server(self) -> ServerInfo | None:
        return self._server

    @property
    def last_error(self) -> McpError | None:
        return self._failure

    # --- lifecycle -------------------------------------------------------------------

    async def connect(self) -> ServerInfo:
        """Open the transport and complete the protocol handshake."""
        if self._status in ("connecting", "connected"):
            raise McpError("The connection is already open.")
        self._status = "connecting"
        self._closing = False
        self._failure = None
        self._server = None
        self._queue = asyncio.Queue()
        self._ready = asyncio.get_running_loop().create_future()
        self._owner = asyncio.create_task(self._run(), name=f"mcp-connection:{self._name}")
        try:
            await asyncio.wait_for(asyncio.shield(self._ready), timeout=self._connect_timeout_s)
        except TimeoutError:
            await self._stop_owner()
            self._status = "failed"
            self._failure = McpServerUnavailableError(
                f"The MCP server did not answer within {self._connect_timeout_s:g} seconds."
            )
            raise self._failure from None
        except McpError:
            await self._stop_owner()
            self._status = "failed"
            raise
        assert self._server is not None  # noqa: S101 - set before `_ready` resolves
        return self._server

    async def reconnect(self) -> ServerInfo:
        """Start a fresh session with the server.

        Whatever the server kept for the old session is gone, so this is only
        safe when the caller has no state there worth keeping.
        """
        await self.close()
        self._status = "disconnected"
        return await self.connect()

    async def close(self) -> None:
        """End the session. Safe to call more than once and in any state."""
        self._closing = True
        await self._stop_owner()
        self._status = "closed"

    async def _stop_owner(self) -> None:
        owner, self._owner = self._owner, None
        if owner is None:
            return
        if not owner.done():
            self._queue.put_nowait(None)
            try:
                await asyncio.wait_for(asyncio.shield(owner), timeout=_STUCK_GRACE_S)
            except (TimeoutError, asyncio.CancelledError):
                owner.cancel()
        with contextlib.suppress(BaseException):
            await owner

    # --- requests --------------------------------------------------------------------

    async def list_tools(self) -> list[ToolSpec]:
        """Ask the server which tools it has. Follows pagination."""

        async def run(session: ClientSession) -> list[ToolSpec]:
            tools: list[ToolSpec] = []
            cursor: str | None = None
            for _ in range(_MAX_TOOL_PAGES):
                page = await session.list_tools(
                    params=types.PaginatedRequestParams(cursor=cursor) if cursor else None
                )
                tools.extend(_tool_spec(tool) for tool in page.tools)
                cursor = page.next_cursor
                if not cursor:
                    return tools
            raise McpDiscoveryError("The server's tool list did not end.")

        try:
            result: list[ToolSpec] = await self._submit(run, self._call_timeout_s)
        except McpDiscoveryError:
            raise
        except (McpProtocolError, McpTimeoutError) as exc:
            raise McpDiscoveryError(f"Tool discovery failed: {exc.message}") from exc
        return result

    async def call_tool(
        self, name: str, arguments: Mapping[str, Any], *, timeout_s: float | None = None
    ) -> RawToolResult:
        """Invoke a tool. Arguments are sent as given; validate them first."""
        timeout = timeout_s if timeout_s is not None else self._call_timeout_s

        async def run(session: ClientSession) -> RawToolResult:
            result = await session.call_tool(name, dict(arguments), read_timeout_seconds=timeout)
            return parse_tool_result(result)

        result: RawToolResult = await self._submit(run, timeout)
        return result

    async def ping(self, *, timeout_s: float = 5.0) -> float:
        """Health check. Returns the round-trip time in seconds."""

        async def run(session: ClientSession) -> float:
            started = time.monotonic()
            await asyncio.wait_for(session.send_ping(), timeout=timeout_s)
            return time.monotonic() - started

        latency: float = await self._submit(run, timeout_s)
        return latency

    async def _submit(
        self, run: Callable[[ClientSession], Awaitable[Any]], timeout_s: float
    ) -> Any:
        if self._status != "connected":
            if self._failure is not None and self._status == "failed":
                raise McpConnectionLostError(self._failure.message)
            raise McpNotConnectedError("The MCP connection is not open.")
        job = _Job(run, asyncio.get_running_loop().create_future())
        self._queue.put_nowait(job)
        try:
            return await asyncio.wait_for(job.future, timeout=timeout_s + _STUCK_GRACE_S)
        except TimeoutError:
            # The owner never produced an answer or an error: the connection is unusable.
            self._failure = McpTimeoutError("The MCP server stopped responding.")
            self._status = "failed"
            owner, self._owner = self._owner, None
            if owner is not None:
                owner.cancel()
            raise self._failure from None

    # --- owner task ------------------------------------------------------------------

    async def _run(self) -> None:
        ready = self._ready
        assert ready is not None  # noqa: S101
        current: _Job | None = None
        failure: McpError | None = None
        try:
            async with (
                self._transport() as streams,
                ClientSession(
                    streams[0],
                    streams[1],
                    read_timeout_seconds=self._call_timeout_s,
                    client_info=self._client_info,
                ) as session,
            ):
                try:
                    init = await session.initialize()
                except MCPError as exc:
                    raise McpInitializationError(
                        f"The MCP handshake failed: {exc.message}"
                    ) from exc
                self._server = _server_info(init)
                self._status = "connected"
                if not ready.done():
                    ready.set_result(None)
                while True:
                    current = await self._queue.get()
                    if current is None:
                        break
                    await self._serve(session, current)
                    current = None
        except McpError as exc:
            failure = exc
        except asyncio.CancelledError:
            # A cancellation that outlives the SDK's contexts came from outside (`close()`
            # or shutdown). A transport failure also cancels this task, but the contexts
            # turn that into the exception group handled below.
            failure = McpConnectionLostError("The MCP connection was closed.")
            raise
        except BaseException as exc:
            failure = _classify(exc, connected=self._status == "connected")
        finally:
            was_connected = self._status == "connected"
            if failure is None:
                failure = McpConnectionLostError("The MCP connection was closed.")
            if not self._closing:
                self._failure = failure
                self._status = "failed"
                logger.warning(
                    "mcp_connection_failed",
                    extra={"connection": self._name, "error_code": failure.code},
                )
            if not ready.done():
                ready.set_exception(failure)
                # Nobody may be waiting any more; do not warn about an unretrieved error.
                ready.exception()
            lost = (
                failure
                if was_connected or isinstance(failure, McpConnectionLostError)
                else McpConnectionLostError(failure.message)
            )
            self._fail_pending(current, lost)

    async def _serve(self, session: ClientSession, job: _Job) -> None:
        try:
            result = await job.run(session)
        except MCPError as exc:
            _resolve(job, error=_map_protocol_error(exc))
        except TimeoutError:
            _resolve(job, error=McpTimeoutError("The MCP server did not answer in time."))
        except McpError as exc:
            _resolve(job, error=exc)
        else:
            _resolve(job, result=result)

    def _fail_pending(self, current: _Job | None, error: McpError) -> None:
        if current is not None:
            _resolve(current, error=error)
        while True:
            try:
                job = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if job is not None:
                _resolve(job, error=error)


def _resolve(job: _Job, *, result: Any = None, error: McpError | None = None) -> None:
    if job.future.done():
        return
    if error is not None:
        job.future.set_exception(error)
    else:
        job.future.set_result(result)


def _map_protocol_error(exc: MCPError) -> McpError:
    if exc.code == _REQUEST_TIMEOUT_CODE:
        return McpTimeoutError("The MCP server did not answer in time.")
    return McpProtocolError(f"The MCP server rejected the request (code {exc.code}).")


def _leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for inner in exc.exceptions for leaf in _leaves(inner)]
    return [exc]


def _classify(exc: BaseException, *, connected: bool) -> McpError:
    """Turn whatever the transport raised into one of our errors, without its details.

    Transport errors can quote URLs and headers, so only the type name is kept.
    """
    leaves = _leaves(exc)
    for leaf in leaves:
        if isinstance(leaf, McpError):
            return leaf
    kinds = ", ".join(sorted({type(leaf).__name__ for leaf in leaves})) or type(exc).__name__
    if connected:
        return McpConnectionLostError(f"The connection to the MCP server was lost ({kinds}).")
    return McpServerUnavailableError(f"The MCP server could not be reached ({kinds}).")


def _server_info(init: Any) -> ServerInfo:
    info = init.server_info
    capabilities = init.capabilities.model_dump(exclude_none=True) if init.capabilities else {}
    return ServerInfo(
        name=str(info.name),
        version=str(info.version),
        protocol_version=str(init.protocol_version),
        capabilities=tuple(sorted(capabilities)),
    )


def _tool_spec(tool: Any) -> ToolSpec:
    annotations = tool.annotations
    return ToolSpec(
        name=str(tool.name),
        title=(annotations.title if annotations and annotations.title else tool.title),
        description=str(tool.description or ""),
        input_schema=dict(tool.input_schema or {}),
        read_only_hint=annotations.read_only_hint if annotations else None,
        destructive_hint=annotations.destructive_hint if annotations else None,
    )
