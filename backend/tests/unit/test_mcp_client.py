"""The MCP client against a mock server: discovery, invocation, failures, cleanup."""

import asyncio

import httpx
import pytest
from mcp import types

from app.mcp.catalog import ToolCatalog, ToolSpec
from app.mcp.connection import McpConnection
from app.mcp.errors import (
    InvalidToolArgumentsError,
    McpConnectionLostError,
    McpDiscoveryError,
    McpNotConnectedError,
    McpServerUnavailableError,
    McpTimeoutError,
    UnknownToolError,
)
from tests.mcp_fakes import FakeBrowser, MockMcpServer, text_result, tool


def _connection(server: MockMcpServer) -> McpConnection:
    return McpConnection(server.transport(), connect_timeout_s=1.0, call_timeout_s=2.0)


def _status(connection: McpConnection) -> str:
    """Read through a function so the type checker does not assume it never changes."""
    return connection.status


def _no_stray_tasks() -> bool:
    return len(asyncio.all_tasks()) == 1


# --- connection, initialization, capabilities ---------------------------------------------


async def test_connects_and_reports_what_the_server_said_about_itself() -> None:
    connection = _connection(MockMcpServer(version="9.9.9"))
    assert _status(connection) == "disconnected"

    server = await connection.connect()

    assert (server.name, server.version) == ("Playwright", "9.9.9")
    assert "tools" in server.capabilities
    assert server.protocol_version
    assert _status(connection) == "connected"
    assert await connection.ping() >= 0
    await connection.close()
    assert _status(connection) == "closed"
    assert _no_stray_tasks()


async def test_unreachable_server_is_reported_as_unavailable_without_transport_details() -> None:
    server = MockMcpServer()
    server.fail_connect = httpx.ConnectError("could not reach http://user:hunter2@runner:9000/mcp")
    connection = _connection(server)

    with pytest.raises(McpServerUnavailableError) as raised:
        await connection.connect()

    assert _status(connection) == "failed"
    assert "hunter2" not in raised.value.message  # only the error type is kept
    assert "ConnectError" in raised.value.message
    assert _no_stray_tasks()


async def test_server_that_never_completes_the_handshake_times_out() -> None:
    server = MockMcpServer()
    server.hang_handshake = True
    connection = McpConnection(server.transport(), connect_timeout_s=0.3)

    with pytest.raises(McpServerUnavailableError, match="did not answer"):
        await connection.connect()

    assert _status(connection) == "failed"
    assert _no_stray_tasks()


async def test_calls_are_refused_before_connecting_and_after_closing() -> None:
    connection = _connection(MockMcpServer())
    with pytest.raises(McpNotConnectedError):
        await connection.list_tools()
    await connection.connect()
    await connection.close()
    await connection.close()  # closing twice is fine
    with pytest.raises(McpNotConnectedError):
        await connection.call_tool("browser_snapshot", {})


# --- tool discovery -----------------------------------------------------------------------


async def test_discovers_tools_with_schemas_and_hints_across_pages() -> None:
    server = MockMcpServer(page_size=5)
    connection = _connection(server)
    await connection.connect()

    tools = await connection.list_tools()

    assert len(tools) == len(server.tools) > 5
    by_name = {t.name: t for t in tools}
    assert by_name["browser_navigate"].argument_names == {"url"}
    assert by_name["browser_snapshot"].read_only_hint is True
    assert by_name["browser_click"].read_only_hint is False
    await connection.close()


async def test_discovery_failure_is_its_own_error() -> None:
    server = MockMcpServer()
    server.fail_discovery = True
    connection = _connection(server)
    await connection.connect()

    with pytest.raises(McpDiscoveryError):
        await connection.list_tools()
    await connection.close()


def _catalog(*tools: types.Tool) -> ToolCatalog:
    return ToolCatalog.build(ToolSpec(t.name, t.description or "", t.input_schema) for t in tools)


def test_catalog_leaves_out_tools_it_could_not_validate_arguments_for() -> None:
    good = tool("good", {"url": {"type": "string"}}, ["url"])
    broken = types.Tool(
        name="broken", input_schema={"type": "object", "properties": {"a": {"type": 7}}}
    )
    not_object = types.Tool(name="not_object", input_schema={"type": "string"})
    twice = tool("twice")

    catalog = _catalog(good, broken, not_object, twice, twice)

    assert catalog.names() == ["good"]
    assert set(catalog.rejected) == {"broken", "not_object", "twice"}
    with pytest.raises(UnknownToolError):
        catalog.get("broken")


# --- invalid arguments --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "problem"),
    [
        ({}, "missing url"),
        ({"url": 5}, "url: must satisfy type 'string'"),
        ({"url": "https://a.example", "filename": "x"}, "unexpected filename"),
    ],
)
def test_arguments_are_checked_against_the_discovered_schema(
    arguments: dict[str, object], problem: str
) -> None:
    catalog = _catalog(tool("browser_navigate", {"url": {"type": "string"}}, ["url"]))
    with pytest.raises(InvalidToolArgumentsError) as raised:
        catalog.validate("browser_navigate", arguments)
    assert problem in raised.value.message


def test_argument_errors_name_fields_but_never_repeat_values() -> None:
    catalog = _catalog(tool("browser_type", {"text": {"type": "string", "maxLength": 3}}, ["text"]))
    with pytest.raises(InvalidToolArgumentsError) as raised:
        catalog.validate("browser_type", {"text": "hunter2-password", "hunter2-key": 1})
    assert "text" in raised.value.message
    assert "hunter2-password" not in raised.value.message


# --- invocation and result parsing --------------------------------------------------------


async def test_invokes_a_tool_and_parses_text_images_and_the_error_flag() -> None:
    browser = FakeBrowser()
    browser.scripted["browser_take_screenshot"] = [
        types.CallToolResult(
            content=[
                types.TextContent(type="text", text="### Result\nscreenshot"),
                types.ImageContent(type="image", data="aGk=", mime_type="image/png"),
            ]
        )
    ]
    browser.scripted["browser_click"] = [text_result("### Error\nRef e9 not found", is_error=True)]
    connection = _connection(MockMcpServer(browser))
    await connection.connect()

    opened = await connection.call_tool("browser_navigate", {"url": "https://shop.example/"})
    shot = await connection.call_tool("browser_take_screenshot", {})
    failed = await connection.call_tool("browser_click", {"target": "e9"})

    assert not opened.is_error and "Page URL: https://shop.example/" in opened.text
    assert [(i.mime_type, i.data_base64) for i in shot.images] == [("image/png", "aGk=")]
    assert failed.is_error and "Ref e9 not found" in failed.text
    assert browser.calls[0] == ("browser_navigate", {"url": "https://shop.example/"})
    await connection.close()


# --- timeouts -----------------------------------------------------------------------------


async def test_a_slow_tool_times_out_and_the_connection_stays_usable() -> None:
    browser = FakeBrowser(delay_s={"browser_wait_for": 5})
    connection = _connection(MockMcpServer(browser))
    await connection.connect()

    with pytest.raises(McpTimeoutError):
        await connection.call_tool("browser_wait_for", {"time": 5}, timeout_s=0.2)

    assert _status(connection) == "connected"
    assert not (await connection.call_tool("browser_snapshot", {})).is_error
    await connection.close()
    assert _no_stray_tasks()


# --- connection interruption and reconnection ---------------------------------------------


async def test_losing_the_server_fails_the_call_in_flight_and_every_later_one() -> None:
    browser = FakeBrowser(delay_s={"browser_wait_for": 5})
    server = MockMcpServer(browser)
    connection = _connection(server)
    await connection.connect()
    in_flight = asyncio.create_task(connection.call_tool("browser_wait_for", {"time": 5}))
    await asyncio.sleep(0.1)

    server.kill()

    with pytest.raises(McpConnectionLostError):
        await in_flight
    assert _status(connection) == "failed"
    assert connection.last_error is not None
    with pytest.raises(McpConnectionLostError):
        await connection.call_tool("browser_snapshot", {})
    await connection.close()
    assert _no_stray_tasks()


async def test_reconnect_opens_a_new_session_with_the_server() -> None:
    server = MockMcpServer()
    connection = _connection(server)
    await connection.connect()
    server.kill()
    await asyncio.sleep(0.1)
    assert _status(connection) == "failed"

    await connection.reconnect()

    assert (_status(connection), server.connections) == ("connected", 2)
    assert not (await connection.call_tool("browser_snapshot", {})).is_error
    await connection.close()
    assert _no_stray_tasks()
