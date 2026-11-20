"""Tool permissions, redaction and reading of results. No server involved."""

import pytest

from app.browser.outcome import classify_error, read_result
from app.browser.policy import (
    PLAYWRIGHT_MCP_RULES,
    SessionGrant,
    ToolCategory,
    ToolPolicy,
)
from app.browser.redaction import MASK, Redactor, sanitize_url
from app.mcp.catalog import ToolCatalog, ToolSpec
from app.mcp.results import RawToolResult
from app.models.project import BROWSER_ACTIONS
from tests.mcp_fakes import PLAYWRIGHT_TOOLS, tool

ALL_ACTIONS = frozenset(BROWSER_ACTIONS)


def _catalog(tools: list | None = None) -> ToolCatalog:  # type: ignore[type-arg]
    return ToolCatalog.build(
        ToolSpec(
            t.name,
            t.description or "",
            t.input_schema,
            read_only_hint=t.annotations.read_only_hint if t.annotations else None,
        )
        for t in (tools or PLAYWRIGHT_TOOLS)
    )


def _exposed(grant: SessionGrant) -> set[str]:
    return set(ToolPolicy().exposure(_catalog(), grant).allowed)


# --- classification -----------------------------------------------------------------------


def test_every_rule_names_a_known_environment_action() -> None:
    for rule in PLAYWRIGHT_MCP_RULES:
        assert rule.action is None or rule.action in BROWSER_ACTIONS, rule.name
        assert rule.url_args <= rule.allowed_args
        # Writing output to a file on the browser host is never an allowed argument.
        assert "filename" not in rule.allowed_args


def test_tools_are_classified_by_what_they_can_do() -> None:
    category = {rule.name: rule.category for rule in PLAYWRIGHT_MCP_RULES}
    assert category["browser_snapshot"] is ToolCategory.READ_ONLY
    assert category["browser_navigate"] is ToolCategory.REVERSIBLE
    assert category["browser_click"] is ToolCategory.STATE_CHANGING
    assert category["browser_handle_dialog"] is ToolCategory.STATE_CHANGING  # can confirm a delete
    assert category["browser_evaluate"] is ToolCategory.SENSITIVE
    assert category["browser_file_upload"] is ToolCategory.SENSITIVE


# --- exposure -----------------------------------------------------------------------------


def test_default_deny_unknown_internal_and_code_running_tools_are_never_offered() -> None:
    exposure = ToolPolicy().exposure(_catalog(), SessionGrant(allowed_actions=ALL_ACTIONS))

    assert "browser_click" in exposure.allowed
    withheld = {name: decision.code for name, decision in exposure.withheld.items()}
    assert withheld["browser_teleport"] == "tool_unknown"  # no rule written for it
    assert withheld["browser_close"] == "tool_not_exposed"
    assert withheld["browser_evaluate"] == "tool_forbidden"
    assert withheld["browser_run_code_unsafe"] == "tool_forbidden"
    assert withheld["browser_file_upload"] == "tool_forbidden"
    assert withheld["browser_network_request"] == "approval_required"


def test_a_tool_the_server_did_not_list_cannot_be_offered() -> None:
    only_snapshot = [t for t in PLAYWRIGHT_TOOLS if t.name == "browser_snapshot"]
    exposure = ToolPolicy().exposure(
        _catalog(only_snapshot), SessionGrant(allowed_actions=ALL_ACTIONS)
    )
    assert set(exposure.allowed) == {"browser_snapshot"}


def test_environment_actions_limit_the_tools() -> None:
    read_only_actions = _exposed(SessionGrant(allowed_actions=frozenset({"navigate", "read"})))
    assert "browser_navigate" in read_only_actions and "browser_snapshot" in read_only_actions
    assert not {"browser_click", "browser_type", "browser_select_option"} & read_only_actions

    no_typing = _exposed(SessionGrant(allowed_actions=ALL_ACTIONS - {"type"}))
    assert "browser_click" in no_typing
    assert not {"browser_type", "browser_fill_form", "browser_press_key"} & no_typing


def test_read_only_environment_gets_no_state_changing_tool_even_if_actions_allow_it() -> None:
    exposed = _exposed(SessionGrant(allowed_actions=ALL_ACTIONS, read_only=True))
    policy = ToolPolicy()
    assert exposed
    assert all(policy.rule(name).category is not ToolCategory.STATE_CHANGING for name in exposed)  # type: ignore[union-attr]


def test_state_changing_tools_wait_for_approval_where_the_environment_requires_it() -> None:
    policy, catalog = ToolPolicy(), _catalog()
    pending = policy.exposure(
        catalog, SessionGrant(allowed_actions=ALL_ACTIONS, requires_approval=True)
    )
    approved = policy.exposure(
        catalog,
        SessionGrant(
            allowed_actions=ALL_ACTIONS, requires_approval=True, state_changes_approved=True
        ),
    )

    assert pending.withheld["browser_click"].needs_approval
    assert "browser_snapshot" in pending.allowed  # looking is still fine
    assert "browser_click" in approved.allowed


def test_sensitive_tool_needs_its_own_approval_and_forbidden_ones_cannot_be_approved() -> None:
    grant = SessionGrant(
        allowed_actions=ALL_ACTIONS,
        approved_tools=frozenset({"browser_network_request", "browser_evaluate"}),
    )
    exposed = _exposed(grant)
    assert "browser_network_request" in exposed
    assert "browser_evaluate" not in exposed


def test_only_requested_and_administrator_enabled_tools_are_offered() -> None:
    grant = SessionGrant(
        allowed_actions=ALL_ACTIONS,
        requested_tools=frozenset({"browser_navigate", "browser_click", "browser_evaluate"}),
        organization_tools=frozenset({"browser_navigate", "browser_snapshot"}),
    )
    assert _exposed(grant) == {"browser_navigate"}


def test_a_changed_server_contract_withholds_the_tool() -> None:
    no_longer_read_only = [
        tool("browser_snapshot", {"target": {}, "depth": {}, "boxes": {}}, read_only=False)
    ]
    renamed_argument = [tool("browser_navigate", {"address": {"type": "string"}})]
    policy, grant = ToolPolicy(), SessionGrant(allowed_actions=ALL_ACTIONS)

    for tools, name in (
        (no_longer_read_only, "browser_snapshot"),
        (renamed_argument, "browser_navigate"),
    ):
        exposure = policy.exposure(_catalog(tools), grant)
        assert exposure.withheld[name].code == "tool_contract_mismatch"
        assert not exposure.allowed


# --- redaction ----------------------------------------------------------------------------


def test_secret_values_are_removed_in_plain_and_encoded_forms() -> None:
    secret = 'p@ss word&"<1>'
    redactor = Redactor([secret])
    samples = [
        f"fill('{secret}')",
        "https://shop.example/?next=p%40ss%20word%26%22%3C1%3E",
        "body=p%40ss+word%26%22%3C1%3E",
        '{"password": "p@ss word&\\"<1>"}',
        "<input value='p@ss word&amp;&quot;&lt;1&gt;'>",
    ]
    for sample in samples:
        cleaned = redactor.text(sample)
        assert MASK in cleaned, sample
        assert "ss word" not in cleaned and "ss+word" not in cleaned and "ss%20word" not in cleaned


def test_token_shapes_and_sensitive_headers_are_masked_without_knowing_the_value() -> None:
    redactor = Redactor()
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N"
    text = redactor.text(
        f"Authorization: Bearer abcdef0123456789\nSet-Cookie: session=topsecret; HttpOnly\ntoken {jwt}"
    )
    assert "abcdef0123456789" not in text and "topsecret" not in text and jwt not in text


def test_urls_lose_credentials_and_sensitive_parameters_but_stay_readable() -> None:
    cleaned = sanitize_url(
        "https://user:pw@shop.example/cb?code=abc123&page=2&access_token=zzz#id_token=q"
    )
    assert cleaned.startswith("https://shop.example/cb?")
    assert "page=2" in cleaned
    for leaked in ("user:pw", "abc123", "zzz", "id_token=q"):
        assert leaked not in cleaned
    assert (
        sanitize_url("https://shop.example/products?page=2")
        == "https://shop.example/products?page=2"
    )


def test_arguments_are_redacted_recursively_and_shortened_for_storage() -> None:
    redactor = Redactor(["hunter2"])
    stored = redactor.arguments(
        {
            "text": "hunter2",
            "fields": [{"name": "Password", "value": "hunter2"}],
            "note": "x" * 1000,
        }
    )
    assert stored["text"] == MASK
    assert stored["fields"][0]["value"] == MASK
    assert len(stored["note"]) < 400


# --- reading results ----------------------------------------------------------------------

# Copied from a real Playwright MCP 0.0.86 server.
_CLICK = """### Page
- Page URL: http://demo-app:8080/dialog
- Console: 1 errors, 0 warnings
### Modal state
- ["confirm" dialog with message "Delete the basket?"]: can be handled by browser_handle_dialog
### Snapshot
- [Snapshot](.playwright-mcp/page-2026-11-19T04-37-13-241Z.yml)"""
_DOWNLOAD = """### Page
- Page URL: http://demo-app:8080/account
- Page Title: Your account
### Events
- Downloading file report.txt ...
- Downloaded file report.txt to ".playwright-mcp/report.txt\""""


def test_reads_page_dialog_and_download_facts_from_a_result() -> None:
    click = read_result(RawToolResult(False, (_CLICK,)))
    assert click.page_url == "http://demo-app:8080/dialog"
    assert click.page_title is None and click.dialog_open and not click.downloads

    download = read_result(RawToolResult(False, (_DOWNLOAD,)))
    assert download.page_title == "Your account"
    assert download.downloads == ("report.txt",)


def test_a_result_in_an_unexpected_layout_yields_unknown_not_a_guess() -> None:
    reading = read_result(RawToolResult(False, ("The page is at http://evil.example now",)))
    assert reading.page_url is None and not reading.dialog_open and not reading.downloads


@pytest.mark.parametrize(
    ("text", "code"),
    [
        (
            "Error: async initializeServer: Failed to launch chromium because executable doesn't exist",
            "browser_launch_failed",
        ),
        ("TimeoutError: browserBackend.callTool: Timeout 4000ms exceeded.", "browser_timeout"),
        (
            "Error: browserBackend.callTool: net::ERR_BLOCKED_BY_CLIENT at http://example.com/",
            "destination_not_allowed",
        ),
        (
            "Error: browserBackend.callTool: net::ERR_TUNNEL_CONNECTION_FAILED at https://x.example/",
            "destination_not_allowed",
        ),
        (
            "Error: browserBackend.callTool: net::ERR_NAME_NOT_RESOLVED at http://nonexistent.invalid/",
            "navigation_failed",
        ),
        (
            'Error: Access to "file:" protocol is blocked. Attempted URL: "file:///etc/passwd"',
            "navigation_failed",
        ),
        ("Error: Ref e9999 not found in the current page snapshot.", "tool_failed"),
        ("Something nobody has seen before", "tool_failed"),
    ],
)
def test_server_error_texts_map_to_stable_codes(text: str, code: str) -> None:
    assert classify_error(text) == code
