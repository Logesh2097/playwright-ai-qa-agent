"""Reasons a browser action did not happen, or did not finish.

Every one of these means the action must not be counted as performed.
"""


class BrowserError(Exception):
    code = "browser_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class BrowserNotConfiguredError(BrowserError):
    code = "browser_not_configured"


class RunnerUnavailableError(BrowserError):
    """The service that starts browsers could not be reached or refused."""

    code = "runner_unavailable"


class RunnerBusyError(BrowserError):
    code = "runner_busy"


class SessionClosedError(BrowserError):
    code = "session_closed"


class ToolNotPermittedError(BrowserError):
    """The session's permissions do not include this tool. Nothing was sent."""

    code = "tool_not_permitted"


class ApprovalRequiredError(BrowserError):
    """A person with the right role has to approve this first. Nothing was sent."""

    code = "approval_required"


class ScopeViolationError(BrowserError):
    """The address is outside what this environment allows."""

    code = "destination_not_allowed"


class DownloadBlockedError(BrowserError):
    code = "download_blocked"


class BrowserLaunchError(BrowserError):
    code = "browser_launch_failed"


class BrowserClosedError(BrowserError):
    """The browser went away mid-session; its page and login state are gone."""

    code = "browser_closed"


class BrowserTimeoutError(BrowserError):
    """No result in time. The action may or may not have taken effect."""

    code = "browser_timeout"


class NavigationFailedError(BrowserError):
    code = "navigation_failed"


class ToolFailedError(BrowserError):
    """The server ran the tool and reported an error."""

    code = "tool_failed"


class ConnectionFailedError(BrowserError):
    """The MCP connection could not be opened or was lost."""

    code = "mcp_connection_failed"


class LoginFailedError(BrowserError):
    code = "login_failed"
