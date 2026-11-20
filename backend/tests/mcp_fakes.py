"""A mock MCP server and a pretend browser behind it.

The server is a real one, built with the MCP SDK and connected over the SDK's
in-memory streams, so the client under test goes through the genuine protocol:
handshake, discovery, calls and errors. Only the browser is pretend. Its
answers copy the layout Playwright MCP 0.0.86 was observed to produce.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpx
from mcp import types
from mcp.server.lowlevel import Server
from mcp.shared.memory import create_client_server_memory_streams

from app.browser.events import SessionEvent, ToolCallEvent
from app.browser.manager import BrowserSessionManager
from app.browser.services import EgressClient, RunnerClient
from app.mcp.connection import Transport

Handler = Callable[[str, dict[str, Any]], Awaitable[types.CallToolResult]]


def tool(
    name: str,
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
    *,
    read_only: bool = False,
) -> types.Tool:
    schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": properties or {},
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return types.Tool(
        name=name,
        description=name,
        input_schema=schema,
        annotations=types.ToolAnnotations(read_only_hint=read_only, destructive_hint=not read_only),
    )


_S = {"type": "string"}
_N = {"type": "number"}
_B = {"type": "boolean"}

# The subset of Playwright MCP's tools the tests need, with the real argument names.
PLAYWRIGHT_TOOLS: list[types.Tool] = [
    tool("browser_navigate", {"url": _S}, ["url"]),
    tool("browser_navigate_back"),
    tool(
        "browser_snapshot", {"target": _S, "filename": _S, "depth": _N, "boxes": _B}, read_only=True
    ),
    tool(
        "browser_find", {"text": _S, "regex": _S, "maxResults": _N, "filename": _S}, read_only=True
    ),
    tool(
        "browser_take_screenshot",
        {"target": _S, "element": _S, "type": _S, "filename": _S, "fullPage": _B, "scale": _S},
        read_only=True,
    ),
    tool("browser_console_messages", {"level": _S, "all": _B, "filename": _S}, read_only=True),
    tool(
        "browser_network_requests",
        {"static": _B, "filter": _S, "all": _B, "filename": _S},
        read_only=True,
    ),
    tool("browser_wait_for", {"time": _N, "text": _S, "textGone": _S}, read_only=True),
    tool("browser_tabs", {"action": _S, "index": _N, "url": _S, "isolatedContext": _S}, ["action"]),
    tool("browser_resize", {"width": _N, "height": _N}, ["width", "height"]),
    tool("browser_hover", {"target": _S, "element": _S}, ["target"]),
    tool(
        "browser_click",
        {
            "target": _S,
            "element": _S,
            "doubleClick": _B,
            "button": _S,
            "modifiers": {"type": "array"},
        },
        ["target"],
    ),
    tool(
        "browser_drag",
        {"startTarget": _S, "startElement": _S, "endTarget": _S, "endElement": _S},
        ["startTarget", "endTarget"],
    ),
    tool("browser_handle_dialog", {"accept": _B, "promptText": _S}, ["accept"]),
    tool(
        "browser_type",
        {"target": _S, "element": _S, "text": _S, "submit": _B, "slowly": _B},
        ["target", "text"],
    ),
    tool("browser_fill_form", {"fields": {"type": "array"}}, ["fields"]),
    tool("browser_press_key", {"key": _S}, ["key"]),
    tool(
        "browser_select_option",
        {"target": _S, "element": _S, "values": {"type": "array"}},
        ["target", "values"],
    ),
    tool(
        "browser_network_request",
        {"index": _N, "part": _S, "filename": _S},
        ["index"],
        read_only=True,
    ),
    tool("browser_evaluate", {"function": _S}, ["function"]),
    tool("browser_run_code_unsafe", {"code": _S}),
    tool("browser_file_upload", {"paths": {"type": "array"}}),
    tool("browser_close"),
    # Not in the permission table: stands for a tool added by a future server version.
    tool("browser_teleport", {"url": _S}),
]


def text_result(text: str, *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)], is_error=is_error
    )


def page_text(url: str, title: str = "Page", extra: str = "") -> str:
    return f"### Page\n- Page URL: {url}\n- Page Title: {title}\n{extra}"


@dataclass
class FakeBrowser:
    """Pretend browser with a login page at /login. Records every call it receives."""

    username: str = "demo"
    password: str = "s3cret-pass"
    base: str = "https://shop.example"
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    url: str = "about:blank"
    typed: dict[str, str] = field(default_factory=dict)
    signed_in: bool = False
    # Test hooks.
    delay_s: dict[str, float] = field(default_factory=dict)
    redirect_to: dict[str, str] = field(default_factory=dict)
    scripted: dict[str, list[types.CallToolResult]] = field(default_factory=dict)
    echo_typed_text: bool = False

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    async def __call__(self, name: str, args: dict[str, Any]) -> types.CallToolResult:
        self.calls.append((name, args))
        if name in self.delay_s:
            await asyncio.sleep(self.delay_s[name])
        if self.scripted.get(name):
            return self.scripted[name].pop(0)
        if name == "browser_navigate":
            self.url = self.redirect_to.get(args["url"], args["url"])
            if self.url.endswith("/account") and not self.signed_in:
                self.url = f"{self.base}/login"
            return text_result(page_text(self.url, "Sign in" if "login" in self.url else "Shop"))
        if name == "browser_type":
            self.typed[args["target"]] = args["text"]
            code = (
                f"### Ran Playwright code\nfill('{args['text']}')" if self.echo_typed_text else ""
            )
            return text_result(code)
        if name == "browser_click":
            if "Sign in" in args["target"]:
                ok = (
                    self.typed.get('role=textbox[name="Username"]') == self.username
                    and self.typed.get('role=textbox[name="Password"]') == self.password
                )
                self.signed_in = ok
                self.url = f"{self.base}/account" if ok else f"{self.base}/login"
            return text_result(page_text(self.url, "Your account" if self.signed_in else "Sign in"))
        if name == "browser_wait_for":
            wanted = args.get("text", "")
            if self.signed_in and wanted in f"Signed in as {self.username}":
                return text_result("### Result\nWaited for " + wanted)
            return text_result("### Error\nTimeoutError: Timeout 10000ms exceeded.", is_error=True)
        if name == "browser_snapshot":
            body = f"Signed in as {self.username}" if self.signed_in else "Please sign in"
            return text_result(
                page_text(self.url, "Your account" if self.signed_in else "Sign in")
                + f"### Snapshot\n```yaml\n- paragraph: {body}\n```"
            )
        if name == "browser_close":
            self.url = "about:blank"
            return text_result("### Result\nNo open tabs.")
        return text_result("### Result\nok")


class MockMcpServer:
    """A real MCP server in this process. `transport` connects a client to it."""

    def __init__(
        self,
        handler: Handler | None = None,
        *,
        tools: list[types.Tool] | None = None,
        page_size: int | None = None,
        name: str = "Playwright",
        version: str = "0.0.0-mock",
    ) -> None:
        self.handler: Handler = handler or FakeBrowser()
        self.tools = PLAYWRIGHT_TOOLS if tools is None else tools
        self.page_size = page_size
        self.connections = 0
        self.fail_connect: BaseException | None = None
        self.fail_discovery = False
        self.hang_handshake = False
        self._scopes: list[anyio.CancelScope] = []
        self._server = Server(
            name, version=version, on_list_tools=self._list_tools, on_call_tool=self._call_tool
        )

    async def _list_tools(
        self, _ctx: Any, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        if self.fail_discovery:
            raise ValueError("discovery is broken")
        if self.page_size is None:
            return types.ListToolsResult(tools=self.tools)
        start = int(params.cursor) if params and params.cursor else 0
        end = start + self.page_size
        return types.ListToolsResult(
            tools=self.tools[start:end], next_cursor=str(end) if end < len(self.tools) else None
        )

    async def _call_tool(
        self, _ctx: Any, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        return await self.handler(params.name, dict(params.arguments or {}))

    def kill(self) -> None:
        """Drop every open connection, as a crashed server process would."""
        for scope in self._scopes:
            scope.cancel()

    def transport(self, _url: str = "") -> Transport:
        @asynccontextmanager
        async def connect() -> AsyncIterator[Any]:
            if self.fail_connect is not None:
                raise self.fail_connect
            self.connections += 1
            async with create_client_server_memory_streams() as (client, server):
                if self.hang_handshake:
                    yield client
                    return
                async with anyio.create_task_group() as group:
                    self._scopes.append(group.cancel_scope)

                    async def serve() -> None:
                        try:
                            await self._server.run(
                                server[0], server[1], self._server.create_initialization_options()
                            )
                        finally:
                            # The server went away: the client's streams end with it.
                            await server[1].aclose()

                    group.start_soon(serve)
                    try:
                        yield client
                    finally:
                        group.cancel_scope.cancel()

        return connect


@dataclass
class Trace:
    """Recorder that keeps events for assertions."""

    calls: list[ToolCallEvent] = field(default_factory=list)
    sessions: list[SessionEvent] = field(default_factory=list)

    async def tool_call(self, event: ToolCallEvent) -> None:
        self.calls.append(event)

    async def session(self, event: SessionEvent) -> None:
        self.sessions.append(event)

    def dump(self) -> str:
        """Everything that was recorded, as text: for 'the secret is nowhere' assertions."""
        return json.dumps([repr(e) for e in (*self.calls, *self.sessions)])


@dataclass
class FakeInfrastructure:
    """Stand-ins for the runner and the egress proxy, recording what they were asked."""

    requests: list[tuple[str, str, Any]] = field(default_factory=list)
    runner_status: int = 201
    egress_status: int = 204
    blocked: list[dict[str, Any]] = field(default_factory=list)
    running: set[str] = field(default_factory=set)
    registered: set[str] = field(default_factory=set)

    def _runner(self, request: httpx.Request) -> httpx.Response:
        body: Any = json.loads(request.content) if request.content else None
        self.requests.append(("runner", f"{request.method} {request.url.path}", body))
        if request.headers.get("authorization") != "Bearer runner-token":
            return httpx.Response(401)
        if request.method == "POST":
            if self.runner_status != 201:
                return httpx.Response(self.runner_status, json={"detail": "refused"})
            self.running.add(body["session_id"])
            return httpx.Response(201, json={"session_id": body["session_id"], "port": 9000})
        if request.method == "DELETE":
            session_id = request.url.path.rsplit("/", 1)[-1]
            found = session_id in self.running
            self.running.discard(session_id)
            return httpx.Response(204 if found else 404)
        return httpx.Response(200, json={"status": "ok"})

    def _egress(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append(("egress", f"{request.method} {request.url.path}", body))
        session_id = request.url.path.rsplit("/", 1)[-1]
        if request.method == "PUT":
            if self.egress_status != 204:
                return httpx.Response(self.egress_status)
            self.registered.add(session_id)
            return httpx.Response(204)
        self.registered.discard(session_id)
        return httpx.Response(200, json={"blocked": self.blocked})

    def egress_client(self) -> EgressClient:
        return EgressClient(
            "http://egress:8901",
            "egress-token",
            "http://egress:3128",
            transport=httpx.MockTransport(self._egress),
        )

    def manager(self, server: MockMcpServer, **kwargs: Any) -> BrowserSessionManager:
        return BrowserSessionManager(
            RunnerClient(
                "http://runner:8900", "runner-token", transport=httpx.MockTransport(self._runner)
            ),
            self.egress_client(),
            transport_factory=server.transport,
            connect_timeout_s=kwargs.pop("connect_timeout_s", 2.0),
            **kwargs,
        )

    def order(self) -> list[str]:
        return [f"{service} {what.split(' ')[0]}" for service, what, _ in self.requests]


def new_id() -> uuid.UUID:
    return uuid.uuid4()
