"""One browser session: every tool call goes through `BrowserSession.call`.

Order of checks for each call, all before anything is sent to the server:

1. the session is open;
2. the tool is one this session was granted (`ToolPolicy`);
3. only arguments the rule names are present;
4. the arguments fit the schema the server published;
5. any address among them is inside the environment's allowed origins.

After the call the result is read for signs that the browser left the allowed
origins, started a download or lost its page; any of these ends the session.
A call that fails at any point raises: there is no return value that could be
mistaken for success.
"""

import contextlib
import ipaddress
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from app.browser.errors import (
    ApprovalRequiredError,
    BrowserClosedError,
    BrowserError,
    BrowserLaunchError,
    BrowserTimeoutError,
    ConnectionFailedError,
    DownloadBlockedError,
    NavigationFailedError,
    ScopeViolationError,
    SessionClosedError,
    ToolFailedError,
    ToolNotPermittedError,
)
from app.browser.events import CallStatus, Recorder, SessionEvent, ToolCallEvent
from app.browser.outcome import Reading, classify_error, read_result
from app.browser.policy import Exposure, ToolRule
from app.browser.redaction import Redactor, sanitize_url
from app.core.security.url_safety import (
    DestinationPolicy,
    Origin,
    UrlNotAllowedError,
    address_block_reason,
    parse_origin,
)
from app.mcp.catalog import ToolCatalog
from app.mcp.connection import McpConnection
from app.mcp.errors import (
    InvalidToolArgumentsError,
    McpConnectionLostError,
    McpError,
    McpNotConnectedError,
    McpTimeoutError,
)
from app.mcp.results import ImageBlock

SessionState = Literal["open", "failed", "closed"]
_MAX_TEXT_CHARS = 100_000
_CLOSE_TOOL = "browser_close"

_ERRORS: dict[str, type[BrowserError]] = {
    "browser_launch_failed": BrowserLaunchError,
    "browser_timeout": BrowserTimeoutError,
    "browser_closed": BrowserClosedError,
    "destination_not_allowed": ScopeViolationError,
    "navigation_failed": NavigationFailedError,
}


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """Result of a call that the server performed without reporting an error.

    This says the action ran. Whether the application then behaved correctly is
    for an assertion to decide, not for this object.
    """

    tool: str
    text: str
    page_url: str | None
    page_title: str | None
    dialog_open: bool
    images: tuple[ImageBlock, ...]
    duration_ms: int


@dataclass(slots=True)
class SessionStats:
    performed: int = 0
    failed: int = 0
    refused: int = 0


def check_destination(url: str, policy: DestinationPolicy) -> Origin:
    """Is this address one the environment allows? Raises `ScopeViolationError`.

    Only the address itself is judged here. Name resolution and the resolved
    addresses are checked where the connection is made, by the egress proxy.
    """
    try:
        origin = parse_origin(url, allow_http=policy.allow_http)
    except UrlNotAllowedError as exc:
        raise ScopeViolationError(str(exc), code=exc.code) from None
    if origin not in policy.allowed_origins:
        raise ScopeViolationError(
            f"{origin} is not in this environment's list of allowed origins.",
            code="origin_not_allowlisted",
        )
    try:
        literal = ipaddress.ip_address(origin.host.strip("[]"))
    except ValueError:
        return origin
    reason = address_block_reason(literal, allow_private=policy.allow_private_network)
    if reason is not None:
        raise ScopeViolationError(
            f"{origin.host} is a {reason}, which is not allowed.", code="address_not_allowed"
        )
    return origin


class BrowserSession:
    def __init__(
        self,
        *,
        session_id: uuid.UUID,
        connection: McpConnection,
        catalog: ToolCatalog,
        exposure: Exposure,
        destinations: DestinationPolicy,
        recorder: Recorder,
        redactor: Redactor | None = None,
        call_timeout_s: float = 45.0,
    ) -> None:
        self.id = session_id
        self._connection = connection
        self._catalog = catalog
        self._exposure = exposure
        self._destinations = destinations
        self._recorder = recorder
        self._redactor = redactor or Redactor()
        self._call_timeout_s = call_timeout_s
        self._state: SessionState = "open"
        self._failure_code: str | None = None
        self._sequence = 0
        self._sent = 0
        self._has_page = False
        self.stats = SessionStats()

    # --- what callers may look at ----------------------------------------------------

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def failure_code(self) -> str | None:
        return self._failure_code

    @property
    def tools(self) -> list[str]:
        """Names of the tools this session may use."""
        return self._exposure.names()

    @property
    def exposure(self) -> Exposure:
        return self._exposure

    @property
    def catalog(self) -> ToolCatalog:
        return self._catalog

    def add_secrets(self, *secrets: str) -> None:
        """Make sure these values never appear in results, logs or errors."""
        self._redactor = self._redactor.extended(*secrets)

    def redact(self, text: str) -> str:
        return self._redactor.text(text)

    # --- the one way to use a tool ---------------------------------------------------

    async def call(
        self,
        tool: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> ToolOutcome:
        arguments = dict(arguments or {})
        self._sequence += 1
        sequence = self._sequence
        started = time.monotonic()
        rule = self._exposure.allowed.get(tool)
        status: CallStatus = "error"
        error: BrowserError | McpError | None = None
        reading: Reading | None = None
        try:
            self._refuse_unless_allowed(tool, rule, arguments)
            assert rule is not None  # noqa: S101 - checked above
            status = "error"
            reading = await self._send(tool, arguments, timeout_s or self._call_timeout_s)
            self._inspect(tool, reading)
            status = "ok"
            self.stats.performed += 1
            return ToolOutcome(
                tool=tool,
                text=self._redactor.text(reading.text)[:_MAX_TEXT_CHARS],
                page_url=sanitize_url(reading.page_url) if reading.page_url else None,
                page_title=self._redactor.text(reading.page_title) if reading.page_title else None,
                dialog_open=reading.dialog_open,
                images=reading.images,
                duration_ms=_elapsed_ms(started),
            )
        except (ToolNotPermittedError, ApprovalRequiredError, SessionClosedError) as exc:
            status, error = "denied", exc
            self.stats.refused += 1
            raise
        except InvalidToolArgumentsError as exc:
            status, error = "denied", exc
            self.stats.refused += 1
            raise
        except (ScopeViolationError, DownloadBlockedError) as exc:
            status, error = "blocked", exc
            self.stats.refused += 1
            raise
        except BrowserTimeoutError as exc:
            status, error = "timeout", exc
            self.stats.failed += 1
            raise
        except BrowserError as exc:
            status, error = "error", exc
            self.stats.failed += 1
            raise
        finally:
            await self._recorder.tool_call(
                ToolCallEvent(
                    session_id=self.id,
                    sequence=sequence,
                    tool=tool,
                    category=rule.category.value if rule else None,
                    arguments=self._redactor.arguments(arguments),
                    status=status,
                    duration_ms=_elapsed_ms(started),
                    error_code=getattr(error, "code", None) if error else None,
                    error_message=self._redactor.summary(error.message) if error else None,
                    page_url=(
                        sanitize_url(reading.page_url) if reading and reading.page_url else None
                    ),
                    result_summary=(
                        self._redactor.summary(_without_snapshot(reading)) if reading else None
                    ),
                )
            )

    def _refuse_unless_allowed(
        self, tool: str, rule: ToolRule | None, arguments: Mapping[str, Any]
    ) -> None:
        if self._state != "open":
            raise SessionClosedError(
                "This browser session has ended.", code=self._failure_code or "session_closed"
            )
        if rule is None:
            decision = self._exposure.withheld.get(tool)
            if decision is not None and decision.needs_approval:
                raise ApprovalRequiredError(decision.reason)
            raise ToolNotPermittedError(
                decision.reason if decision else "The server has no such tool.",
                code=decision.code if decision else "tool_unknown",
            )
        unexpected = sorted(set(arguments) - rule.allowed_args)
        if unexpected:
            raise InvalidToolArgumentsError(
                tool, [f"arguments: {', '.join(unexpected)} may not be used"]
            )
        self._catalog.validate(tool, arguments)
        for name in rule.url_args:
            if name in arguments:
                check_destination(str(arguments[name]), self._destinations)

    async def _send(self, tool: str, arguments: Mapping[str, Any], timeout_s: float) -> Reading:
        try:
            try:
                raw = await self._connection.call_tool(tool, arguments, timeout_s=timeout_s)
            except (McpConnectionLostError, McpNotConnectedError):
                # A new connection is a new, empty browser. That is harmless only
                # while this session has not done anything yet.
                if self._sent > 0:
                    raise
                await self._connection.reconnect()
                await self._recorder.session(SessionEvent(self.id, "reconnected"))
                raw = await self._connection.call_tool(tool, arguments, timeout_s=timeout_s)
        except McpTimeoutError:
            self._sent += 1
            raise BrowserTimeoutError(
                f"{tool} did not finish within {timeout_s:g} seconds."
            ) from None
        except McpError as exc:
            await self._fail("mcp_connection_failed")
            raise ConnectionFailedError(exc.message, code=exc.code) from None
        self._sent += 1
        return read_result(raw)

    def _inspect(self, tool: str, reading: Reading) -> None:
        """Raise unless the result shows the action ran and the browser is where it may be."""
        if reading.downloads:
            self._state, self._failure_code = "failed", "download_blocked"
            raise DownloadBlockedError("The page started a file download, which is not allowed.")
        if reading.is_error:
            message = self._redactor.summary(reading.error_text or "", max_chars=400)
            code = classify_error(reading.error_text or "")
            if code in ("browser_launch_failed", "browser_closed"):
                self._state, self._failure_code = "failed", code
            raise _ERRORS.get(code, ToolFailedError)(message, code=code)
        url = reading.page_url
        if url is None:
            return
        scheme = urlsplit(url).scheme.lower()
        if scheme in ("http", "https"):
            try:
                check_destination(url, self._destinations)
            except ScopeViolationError:
                self._state, self._failure_code = "failed", "destination_not_allowed"
                raise ScopeViolationError(
                    "The browser ended up outside this environment's allowed origins.",
                    code="left_allowed_origins",
                ) from None
            self._has_page = True
        elif url == "about:blank" and self._has_page and tool not in ("browser_tabs",):
            # Playwright MCP relaunches a dead browser silently; an empty page where
            # there was one is the only sign.
            self._state, self._failure_code = "failed", "browser_closed"
            raise BrowserClosedError("The browser closed unexpectedly; its state is lost.")

    # --- lifecycle -------------------------------------------------------------------

    async def _fail(self, code: str) -> None:
        if self._state == "open":
            self._state, self._failure_code = "failed", code
            await self._recorder.session(SessionEvent(self.id, "failed", {"error_code": code}))

    async def close(self) -> None:
        """Close the browser and the connection. Never raises; safe to repeat."""
        if self._state == "closed":
            return
        previous, self._state = self._state, "closed"
        if self._connection.status == "connected" and _CLOSE_TOOL in self._catalog:
            with contextlib.suppress(McpError):
                await self._connection.call_tool(_CLOSE_TOOL, {}, timeout_s=5.0)
        await self._connection.close()
        await self._recorder.session(
            SessionEvent(
                self.id,
                "closed",
                {
                    "performed": self.stats.performed,
                    "failed": self.stats.failed,
                    "refused": self.stats.refused,
                    "ended_as": previous if previous != "open" else "ok",
                },
            )
        )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _without_snapshot(reading: Reading) -> str:
    """Page contents can be large and can hold personal data: keep only the outline."""
    if reading.is_error:
        return reading.error_text or ""
    kept = [
        f"{name}: {body}"
        for name, body in reading.sections.items()
        if name in ("Page", "Events", "Modal state", "Open tabs")
    ]
    return " | ".join(kept) if kept else f"{len(reading.text)} characters of output"
