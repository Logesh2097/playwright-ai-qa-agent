"""Tool selection: which tool does which part of the work. A fixed policy, not a model's choice.

    Looking at the application before tests exist   Playwright MCP, read-only tools
    Running approved, repeatable test cases          the deterministic Playwright engine
    Backend and API checks                           the engine's API steps (no page needed)

Playwright MCP is never used to execute an approved case, and the engine is
never used to wander around an application. No tool is handed to a model: the
model writes content, and code decides what is called.
"""

from app.testing.automation import Script
from app.workflow.state import ToolChoice


def discovery_tool(*, browser_available: bool, allowed_actions: frozenset[str]) -> ToolChoice:
    if browser_available and {"navigate", "read"} <= allowed_actions:
        return "mcp_read_only"
    return "none"


def execution_tool_for_types(test_types: list[str]) -> ToolChoice:
    """Before scripts exist: what the kinds of test asked for will need."""
    return "api_engine" if test_types and set(test_types) <= {"api"} else "playwright_engine"


def execution_tool_for_script(script: Script | None) -> ToolChoice:
    """Once a script exists: what it actually uses."""
    if script is None:
        return "none"
    steps = [*script.steps, *script.cleanup]
    api_only = script.login_as is None and all(
        step.action in ("api_request", "assert_api_status", "assert_api_json", "assert_expression")
        for step in steps
    )
    return "api_engine" if api_only else "playwright_engine"
