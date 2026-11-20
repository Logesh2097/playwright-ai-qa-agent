"""Which MCP tools a browser session may use.

Default deny. A tool is offered to a session only if all of these hold:

1. the server listed it during discovery, with a usable schema;
2. it has a rule below, and the server's own hints do not contradict the rule;
3. the environment allows the kind of action it performs;
4. the workflow asked for it, and an administrator has not switched it off.

A server upgrade that adds or renames tools therefore changes nothing until a
rule is written for the new tool. Arguments are restricted the same way: an
argument a rule does not name is refused, so a new option (for example one that
writes a file) cannot be reached by accident.

The rule table was written against the tool list of Playwright MCP 0.0.86 as
discovered from a running server. `tests/integration/test_browser_real.py`
fails if the pinned server and this table disagree.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from app.mcp.catalog import ToolCatalog, ToolSpec


class ToolCategory(StrEnum):
    READ_ONLY = "read_only"  # looks at the page; changes nothing
    REVERSIBLE = "reversible"  # changes what the browser shows, not the application's data
    STATE_CHANGING = "state_changing"  # can submit, save, delete or send
    SENSITIVE = "sensitive"  # can run code, touch files or read raw traffic


@dataclass(frozen=True, slots=True)
class ToolRule:
    name: str
    category: ToolCategory
    # The environment action (see `BROWSER_ACTIONS`) this tool needs. None: no
    # environment setting grants it.
    action: str | None
    allowed_args: frozenset[str] = frozenset()
    # Arguments that carry an address the browser will open.
    url_args: frozenset[str] = frozenset()
    # Used by the session itself (for example to close the browser); never offered.
    internal: bool = False
    # Sensitive tools that no approval can unlock yet.
    grantable: bool = True


def _rule(
    name: str,
    category: ToolCategory,
    action: str | None,
    args: Iterable[str] = (),
    *,
    url_args: Iterable[str] = (),
    internal: bool = False,
    grantable: bool = True,
) -> ToolRule:
    return ToolRule(
        name, category, action, frozenset(args), frozenset(url_args), internal, grantable
    )


_R, _V, _S, _X = (
    ToolCategory.READ_ONLY,
    ToolCategory.REVERSIBLE,
    ToolCategory.STATE_CHANGING,
    ToolCategory.SENSITIVE,
)

# `filename` (save output to a file on the browser host) and `isolatedContext` are
# deliberately absent from every argument list.
PLAYWRIGHT_MCP_RULES: tuple[ToolRule, ...] = (
    _rule("browser_snapshot", _R, "read", ["target", "depth", "boxes"]),
    _rule("browser_find", _R, "read", ["text", "regex", "maxResults"]),
    _rule(
        "browser_take_screenshot", _R, "read", ["target", "element", "type", "fullPage", "scale"]
    ),
    _rule("browser_console_messages", _R, "read", ["level", "all"]),
    _rule("browser_network_requests", _R, "read", ["static", "filter", "all"]),
    _rule("browser_wait_for", _R, "read", ["time", "text", "textGone"]),
    _rule("browser_navigate", _V, "navigate", ["url"], url_args=["url"]),
    _rule("browser_navigate_back", _V, "navigate"),
    _rule("browser_tabs", _V, "navigate", ["action", "index", "url"], url_args=["url"]),
    _rule("browser_resize", _V, "read", ["width", "height"]),
    _rule("browser_hover", _V, "click", ["target", "element"]),
    _rule(
        "browser_click", _S, "click", ["target", "element", "doubleClick", "button", "modifiers"]
    ),
    _rule("browser_drag", _S, "click", ["startTarget", "startElement", "endTarget", "endElement"]),
    _rule("browser_handle_dialog", _S, "click", ["accept", "promptText"]),
    _rule("browser_type", _S, "type", ["target", "element", "text", "submit", "slowly"]),
    _rule("browser_fill_form", _S, "type", ["fields"]),
    _rule("browser_press_key", _S, "type", ["key"]),
    _rule("browser_select_option", _S, "select", ["target", "element", "values"]),
    # Request and response bodies and headers, which can include session tokens.
    _rule("browser_network_request", _X, "read", ["index", "part"]),
    # Arbitrary code in the page or in the automation process, and files on the
    # browser host: nothing here can check what they would do.
    _rule("browser_evaluate", _X, None, grantable=False),
    _rule("browser_run_code_unsafe", _X, None, grantable=False),
    _rule("browser_file_upload", _X, "upload", grantable=False),
    _rule("browser_drop", _X, "upload", grantable=False),
    _rule("browser_close", _V, None, internal=True),
)


@dataclass(frozen=True, slots=True)
class SessionGrant:
    """What one session is allowed to do. Built by the caller from the environment."""

    allowed_actions: frozenset[str]
    read_only: bool = False
    # State-changing tools need an approval on this environment.
    requires_approval: bool = False
    state_changes_approved: bool = False
    # Sensitive tools individually approved for this session.
    approved_tools: frozenset[str] = frozenset()
    # Tools the workflow needs. None: everything otherwise permitted.
    requested_tools: frozenset[str] | None = None
    # Administrator's restriction for the organization. None: no restriction.
    organization_tools: frozenset[str] | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    code: str = "ok"
    reason: str = ""

    @property
    def needs_approval(self) -> bool:
        return self.code == "approval_required"


_ALLOW = Decision(True)


@dataclass(frozen=True, slots=True)
class Exposure:
    """The outcome of applying the policy to what a server discovered."""

    allowed: Mapping[str, ToolRule]
    withheld: Mapping[str, Decision] = field(default_factory=dict)

    def names(self) -> list[str]:
        return sorted(self.allowed)


class ToolPolicy:
    def __init__(self, rules: Iterable[ToolRule] = PLAYWRIGHT_MCP_RULES) -> None:
        self._rules = {rule.name: rule for rule in rules}

    def rules(self) -> list[ToolRule]:
        return sorted(self._rules.values(), key=lambda rule: rule.name)

    def rule(self, name: str) -> ToolRule | None:
        return self._rules.get(name)

    def check_discovered(self, tool: ToolSpec) -> Decision:
        """Does a discovered tool match the rule written for it?"""
        rule = self._rules.get(tool.name)
        if rule is None:
            return Decision(False, "tool_unknown", "No rule has been written for this tool.")
        if rule.category is ToolCategory.READ_ONLY and tool.read_only_hint is False:
            return Decision(
                False,
                "tool_contract_mismatch",
                "The server no longer describes this tool as read-only.",
            )
        missing = (rule.allowed_args | rule.url_args) - tool.argument_names
        if missing:
            return Decision(
                False,
                "tool_contract_mismatch",
                f"The server's tool lacks expected arguments: {', '.join(sorted(missing))}.",
            )
        return _ALLOW

    def decide(self, rule: ToolRule, grant: SessionGrant) -> Decision:
        """May a session with this grant use the tool at all?"""
        if rule.internal:
            return Decision(False, "tool_not_exposed", "This tool is not available to workflows.")
        if grant.organization_tools is not None and rule.name not in grant.organization_tools:
            return Decision(False, "tool_disabled", "An administrator has switched this tool off.")
        if grant.requested_tools is not None and rule.name not in grant.requested_tools:
            return Decision(False, "tool_not_requested", "This workflow does not use this tool.")
        if rule.category is ToolCategory.SENSITIVE:
            if not rule.grantable:
                return Decision(False, "tool_forbidden", "This tool cannot be enabled.")
            if rule.name not in grant.approved_tools:
                return Decision(
                    False, "approval_required", "This tool needs approval for each session."
                )
        if rule.action is None or rule.action not in grant.allowed_actions:
            return Decision(
                False, "action_not_allowed", "The environment does not allow this kind of action."
            )
        if rule.category is ToolCategory.STATE_CHANGING:
            if grant.read_only:
                return Decision(False, "environment_read_only", "The environment is read-only.")
            if grant.requires_approval and not grant.state_changes_approved:
                return Decision(
                    False,
                    "approval_required",
                    "Changing data in this environment needs approval.",
                )
        return _ALLOW

    def exposure(self, catalog: ToolCatalog, grant: SessionGrant) -> Exposure:
        allowed: dict[str, ToolRule] = {}
        withheld: dict[str, Decision] = {}
        for tool in catalog.tools():
            decision = self.check_discovered(tool)
            rule = self._rules.get(tool.name)
            if decision.allowed and rule is not None:
                decision = self.decide(rule, grant)
            if decision.allowed and rule is not None:
                allowed[tool.name] = rule
            else:
                withheld[tool.name] = decision
        for name, reason in catalog.rejected.items():
            withheld[name] = Decision(False, "tool_schema_invalid", reason)
        return Exposure(allowed, withheld)
