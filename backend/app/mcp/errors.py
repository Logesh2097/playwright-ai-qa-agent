"""Errors raised by the MCP client. None of them carries tool arguments."""


class McpError(Exception):
    """Base class. `code` is stable and safe to store or show."""

    code = "mcp_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class McpServerUnavailableError(McpError):
    """The server could not be reached, or did not answer the handshake in time."""

    code = "mcp_server_unavailable"


class McpInitializationError(McpError):
    """The server answered, but the protocol handshake failed."""

    code = "mcp_initialization_failed"


class McpDiscoveryError(McpError):
    """The list of tools could not be obtained or was unusable."""

    code = "mcp_discovery_failed"


class McpNotConnectedError(McpError):
    code = "mcp_not_connected"


class McpConnectionLostError(McpError):
    """The connection broke after it had been established."""

    code = "mcp_connection_lost"


class McpTimeoutError(McpError):
    """No answer within the time allowed. The call may still have had an effect."""

    code = "mcp_timeout"


class McpProtocolError(McpError):
    """The server rejected the request at protocol level."""

    code = "mcp_protocol_error"


class UnknownToolError(McpError):
    code = "mcp_unknown_tool"


class InvalidToolArgumentsError(McpError):
    """Arguments do not match the tool's published input schema.

    `problems` names the offending fields and rules, never the values supplied.
    """

    code = "mcp_invalid_arguments"

    def __init__(self, tool: str, problems: list[str]) -> None:
        super().__init__(f"Invalid arguments for {tool}: " + "; ".join(problems))
        self.tool = tool
        self.problems = problems
