"""The step schema, the checks made before a run, business-rule expressions and verdicts.

Nothing here needs a browser or any service.
"""

import json
import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.core.security.url_safety import parse_origin
from app.engine.client import EngineClient, EngineError
from app.engine.matching import json_pointer, matches, url_subject
from app.engine.protocol import RunConfig, RunFinished, RunPlan, StepFinished
from app.engine.server import run_budget_s
from app.engine.values import Values
from app.services.evidence import EvidenceStore
from app.services.test_data import looks_like_real_card
from app.testing.automation import Locator, Script, TextMatch, changes_state, permission_for
from app.testing.automation_checks import ScriptFacts, check_script
from app.testing.expressions import ExpressionError, evaluate, names_used, parse, to_number
from app.testing.verdicts import execution_verdict, totals

BUTTON = {"by": "role", "role": "button", "name": "Save"}
CHECK = {"action": "assert_visible", "locator": BUTTON}


def script(*steps: dict[str, Any], **more: Any) -> Script:
    return Script.model_validate({"steps": list(steps), **more})


def facts(**overrides: Any) -> ScriptFacts:
    values: dict[str, Any] = {
        "allowed_origins": frozenset({parse_origin("https://shop.example")}),
        "allowed_actions": frozenset(
            {"navigate", "read", "click", "type", "select", "upload", "api_read", "api_write"}
        ),
        "env_names": frozenset({"REGION"}),
        "data_names": frozenset({"bill_amount"}),
        "data_set_names": frozenset({"checkout"}),
        "credentials": {"customer": frozenset({"username", "password"})},
        "login_roles": frozenset({"customer"}),
        "file_names": frozenset({"receipt.pdf"}),
    }
    values.update(overrides)
    return ScriptFacts(**values)


def codes(checked: Script, **overrides: Any) -> set[str]:
    return {
        issue.code
        for issue in check_script(checked, facts(**overrides))
        if issue.severity == "blocking"
    }


# --- every action is validated against a strict schema ------------------------------------

VALID_STEPS: list[dict[str, Any]] = [
    {"action": "navigate", "url": "/login"},
    {"action": "click", "locator": BUTTON},
    {"action": "fill", "locator": {"by": "label", "value": "Email"}, "value": "a@b.example"},
    {"action": "clear", "locator": {"by": "placeholder", "value": "Search"}},
    {"action": "select_option", "locator": {"by": "label", "value": "Country"}, "label": "India"},
    {"action": "check", "locator": {"by": "label", "value": "Agree"}},
    {"action": "uncheck", "locator": {"by": "label", "value": "Agree"}},
    {"action": "select_radio", "locator": {"by": "label", "value": "Express"}},
    {
        "action": "upload_file",
        "locator": {"by": "label", "value": "Receipt"},
        "file": "receipt.pdf",
    },
    {"action": "wait_for_element", "locator": BUTTON, "state": "hidden"},
    {"action": "wait_for_navigation", "url": {"equals": "/account"}},
    {
        "action": "read_text",
        "locator": {"by": "test_id", "value": "total"},
        "save_as": "total",
        "as_number": True,
    },
    {
        "action": "read_attribute",
        "locator": BUTTON,
        "attribute": "aria-pressed",
        "save_as": "pressed",
    },
    {"action": "assert_visible", "locator": BUTTON},
    {"action": "assert_hidden", "locator": BUTTON},
    {"action": "assert_exists", "locator": BUTTON},
    {"action": "assert_absent", "locator": BUTTON},
    {"action": "assert_count", "locator": {"by": "role", "role": "row"}, "count": 3},
    {"action": "assert_enabled", "locator": BUTTON},
    {"action": "assert_disabled", "locator": BUTTON},
    {"action": "assert_checked", "locator": {"by": "label", "value": "Agree"}, "checked": False},
    {"action": "assert_text", "locator": BUTTON, "text": {"contains": "Save"}},
    {
        "action": "assert_value",
        "locator": {"by": "label", "value": "Email"},
        "value": {"equals": ""},
    },
    {"action": "assert_validation_message", "locator": {"by": "label", "value": "Email"}},
    {"action": "assert_url", "url": {"matches": "^/orders/\\d+$"}},
    {"action": "assert_title", "title": {"equals": "Orders"}},
    {
        "action": "api_request",
        "method": "POST",
        "url": "/api/orders",
        "json_body": {"item": "tea"},
        "save_as": "made",
    },
    {"action": "assert_api_status", "response": "made", "status": [200, 201]},
    {"action": "assert_api_json", "response": "made", "pointer": "/item", "equals": "tea"},
    {"action": "assert_api_json", "response": "made", "pointer": "/deleted_at", "equals": None},
    {"action": "assert_expression", "expression": "total == floor(data.bill_amount / 10)"},
    {"action": "screenshot", "name": "after-save", "full_page": True},
]


@pytest.mark.parametrize("step", VALID_STEPS, ids=lambda s: s["action"])
def test_every_supported_action_has_a_schema(step: dict[str, Any]) -> None:
    parsed = script(step, CHECK).steps[0]
    assert parsed.action == step["action"]


def test_all_actions_of_the_brief_are_covered() -> None:
    assert {s["action"] for s in VALID_STEPS} >= {
        "navigate", "click", "fill", "clear", "select_option", "check", "uncheck", "select_radio",
        "upload_file", "wait_for_element", "wait_for_navigation", "read_text", "read_attribute",
        "assert_visible", "assert_enabled", "assert_url", "assert_title", "assert_api_status",
        "assert_api_json", "screenshot",
    }  # fmt: skip


@pytest.mark.parametrize(
    "step",
    [
        {"action": "evaluate", "script": "document.cookie"},  # no such step
        {"action": "sleep", "ms": 5000},  # no fixed waits
        {"action": "click"},  # nothing to click
        {"action": "click", "locator": BUTTON, "force": True},  # unknown field
        {"action": "click", "locator": "button.save"},  # a raw selector string
        {"action": "fill", "locator": BUTTON},  # no value
        {"action": "select_option", "locator": BUTTON},  # neither value nor label
        {"action": "select_option", "locator": BUTTON, "value": "a", "label": "A"},  # both
        {"action": "click", "locator": BUTTON, "timeout_ms": 10_000_000},
        {"action": "read_text", "locator": BUTTON, "save_as": "Not A Name"},
        {"action": "read_attribute", "locator": BUTTON, "attribute": "on click", "save_as": "x"},
        {"action": "assert_text", "locator": BUTTON, "text": {}},
        {"action": "assert_text", "locator": BUTTON, "text": {"equals": "a", "contains": "a"}},
        {"action": "assert_text", "locator": BUTTON, "text": {"matches": "("}},
        {"action": "assert_count", "locator": BUTTON, "count": -1},
        {"action": "assert_api_status", "response": "r", "status": 99},
        {"action": "assert_api_json", "response": "r", "pointer": "no-slash", "exists": True},
        {"action": "assert_api_json", "response": "r", "pointer": "/a"},
        {
            "action": "assert_api_json",
            "response": "r",
            "pointer": "/a",
            "exists": True,
            "type": "string",
        },
        {"action": "api_request", "method": "TRACE", "url": "/x", "save_as": "r"},
        {"action": "api_request", "url": "/x", "headers": {"Bad Header": "v"}, "save_as": "r"},
        {"action": "assert_expression", "expression": "__import__('os').system('id')"},
        {"action": "screenshot", "name": "../../etc/passwd"},
    ],
)
def test_malformed_and_unknown_steps_are_rejected(step: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        script(step, CHECK)


def test_script_limits_and_cleanup_rules() -> None:
    with pytest.raises(ValidationError):
        Script.model_validate({"steps": []})
    with pytest.raises(ValidationError):
        Script.model_validate({"steps": [CHECK] * 101})
    with pytest.raises(ValidationError, match="cleanup steps undo things"):
        script(CHECK, cleanup=[CHECK])
    with pytest.raises(ValidationError):
        Script.model_validate({"steps": [CHECK], "run_code": "x"})


# --- locators -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "locator",
    [
        {"by": "role", "role": "button"},
        {"by": "role", "role": "textbox", "name": "Email", "exact": True},
        {"by": "label", "value": "Email"},
        {"by": "text", "value": "Welcome"},
        {"by": "placeholder", "value": "Search"},
        {"by": "test_id", "value": "submit"},
        {"by": "css", "value": "table.orders tbody tr:first-child td.total"},
        {
            "by": "role",
            "role": "button",
            "name": "Delete",
            "within": {"by": "role", "role": "row", "name": "Tea"},
        },
    ],
)
def test_supported_locator_strategies(locator: dict[str, Any]) -> None:
    assert Locator.model_validate(locator).describe()


@pytest.mark.parametrize(
    "locator",
    [
        {"by": "xpath", "value": "//button"},
        {"by": "css", "value": "//button[@id='x']"},
        {"by": "css", "value": "(//button)[1]"},
        {"by": "css", "value": "xpath=//button"},
        {"by": "css", "value": "text=Save"},
        {"by": "css", "value": "div >> text=Save"},
        {"by": "role", "role": "button", "value": "x"},
        {"by": "role"},
        {"by": "role", "role": "wizard"},
        {"by": "label"},
        {"by": "label", "value": "Email", "role": "textbox"},
        {
            "by": "css",
            "value": "a",
            "within": {"by": "css", "value": "b", "within": {"by": "css", "value": "c"}},
        },
        {"by": "css", "value": "a", "nth": 2},
    ],
)
def test_xpath_engine_tricks_and_inconsistent_locators_are_rejected(
    locator: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        Locator.model_validate(locator)


def test_css_locators_work_but_are_flagged_as_fragile() -> None:
    issues = check_script(
        script({"action": "assert_visible", "locator": {"by": "css", "value": ".btn-3"}}), facts()
    )
    assert [(i.code, i.severity, i.step) for i in issues] == [
        ("automation_fragile_locator", "warning", 1)
    ]
    assert check_script(script(CHECK), facts()) == []


# --- checks before a run ------------------------------------------------------------------


def test_a_script_without_an_assertion_cannot_run() -> None:
    assert codes(script({"action": "navigate", "url": "/"})) == {"automation_no_assertion"}


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("{{env.REGION}}", True),
        ("{{data.bill_amount}}", True),
        ("{{cred.customer.password}}", True),
        ("{{unique.email}} {{unique.id_second}}", True),
        ("{{env.NOPE}}", False),
        ("{{data.nope}}", False),
        ("{{cred.admin.password}}", False),
        ("{{cred.customer.otp}}", False),
        ("{{unique.uuid}}", False),
        ("{{var.never_saved}}", False),
        ("{{secrets.token}}", False),
        ("{{ data.bill_amount", True),  # not a placeholder at all: literal text
    ],
)
def test_placeholders_must_be_resolvable(value: str, ok: bool) -> None:
    found = codes(script({"action": "fill", "locator": BUTTON, "value": value}, CHECK))
    assert (not found) is ok, found


def test_a_value_can_only_be_used_after_the_step_that_saves_it() -> None:
    read = {"action": "read_text", "locator": BUTTON, "save_as": "label"}
    use = {"action": "fill", "locator": BUTTON, "value": "{{var.label}}"}
    assert not codes(script(read, use, CHECK))
    assert codes(script(use, read, CHECK)) == {"automation_placeholder_unknown"}
    rule = {"action": "assert_expression", "expression": "label == 'Save' and data.bill_amount > 0"}
    assert not codes(script(read, rule))
    assert codes(script(rule)) == {"automation_placeholder_unknown"}
    # Cleanup may use what the steps saved.
    assert not codes(script(read, CHECK, cleanup=[use]))


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("/orders?page=2", True),
        ("https://shop.example/orders", True),
        ("{{env.REGION}}/orders", True),  # known only at run time, checked again there
        ("https://evil.example/", False),
        ("http://shop.example/", False),  # another scheme is another origin
        ("//evil.example/x", False),
        ("orders", False),
        ("javascript:alert(1)", False),
        ("file:///etc/passwd", False),
    ],
)
def test_addresses_must_be_paths_or_allowed_origins(url: str, ok: bool) -> None:
    for step in (
        {"action": "navigate", "url": url},
        {"action": "api_request", "url": url, "save_as": "r"},
    ):
        assert (not codes(script(step, CHECK))) is ok, (url, step["action"])


def test_steps_are_limited_by_what_the_environment_allows() -> None:
    click = {"action": "click", "locator": BUTTON}
    post = {"action": "api_request", "method": "POST", "url": "/api/x", "save_as": "r"}
    get = {"action": "api_request", "url": "/api/x", "save_as": "r"}
    assert codes(script(click, CHECK), read_only=True) == {"automation_environment_read_only"}
    assert codes(script(post, CHECK), read_only=True) == {"automation_environment_read_only"}
    assert not codes(script({"action": "navigate", "url": "/"}, get, CHECK), read_only=True)
    looking_only = frozenset({"navigate", "read"})
    assert codes(script(click, CHECK), allowed_actions=looking_only) == {
        "automation_action_not_allowed"
    }
    assert codes(script(get, CHECK), allowed_actions=looking_only) == {
        "automation_action_not_allowed"
    }
    assert permission_for(script(post).steps[0]) == "api_write" and changes_state(
        script(post).steps[0]
    )
    assert permission_for(script(get).steps[0]) == "api_read" and not changes_state(
        script(get).steps[0]
    )


def test_uploads_logins_data_sets_and_responses_must_exist() -> None:
    upload = {"action": "upload_file", "locator": BUTTON, "file": "invoice.pdf"}
    assert codes(script(upload, CHECK)) == {"automation_file_unknown"}
    assert not codes(script({**upload, "file": "receipt.pdf"}, CHECK))
    assert codes(script(CHECK, login_as="admin")) == {"automation_login_unavailable"}
    assert codes(script(CHECK, login_as="customer"), login_roles=frozenset()) == {
        "automation_login_unavailable"
    }
    assert codes(script(CHECK, data_sets=["missing"])) == {"automation_data_set_unknown"}
    status = {"action": "assert_api_status", "response": "made", "status": 200}
    assert codes(script(status)) == {"automation_response_unknown"}
    assert not codes(script({"action": "api_request", "url": "/x", "save_as": "made"}, status))


def test_issues_name_the_step_they_are_about() -> None:
    checked = script(
        CHECK,
        {"action": "fill", "locator": BUTTON, "value": "{{data.nope}}"},
        cleanup=[{"action": "navigate", "url": "https://evil.example/"}],
    )
    issues = {(i.step, i.code) for i in check_script(checked, facts())}
    assert issues == {(2, "automation_placeholder_unknown"), (3, "automation_url_not_allowed")}
    assert any("Cleanup step 1" in i.message for i in check_script(checked, facts()))


# --- business-rule expressions ------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "values", "expected"),
    [
        ("points == floor(amount / 10)", {"var.points": Decimal(50), "var.amount": "505"}, True),
        ("points == floor(amount / 10)", {"var.points": 51, "var.amount": "505"}, False),
        ("0.1 + 0.2 == 0.3", {}, True),  # decimal arithmetic, not binary floats
        ("round(2.5) == 3 and round(0.125, 2) == 0.13", {}, True),  # half up
        (
            "total == subtotal + tax",
            {"var.total": "₹1,250.50", "var.subtotal": "1,000.50", "var.tax": 250},
            True,
        ),
        (
            "data.limit >= 100 and env.REGION == 'in'",
            {"data.limit": "100", "env.REGION": "in"},
            True,
        ),
        (
            "status in 'active,pending' and not (status == 'pending')",
            {"var.status": "active"},
            True,
        ),
        ("max(a, b) - min(a, b) == abs(a - b)", {"var.a": 3, "var.b": 10}, True),
        ("lower(name) == 'ada' and length(strip(name)) == 3", {"var.name": " Ada "}, False),
        ("upper(strip(name)) == 'ADA'", {"var.name": " Ada "}, True),
        ("1 < x <= 5", {"var.x": 5}, True),
        ("(10 if premium else 5) == 10", {"var.premium": True}, True),
        ("7 // 2 == 3 and 7 % 2 == 1 and ceil(7 / 2) == 4", {}, True),
    ],
)
def test_expressions_evaluate_rules_exactly(
    expression: str, values: dict[str, Any], expected: bool
) -> None:
    assert evaluate(expression, values) is expected


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('id')",
        "open('/etc/passwd').read()",
        "points.__class__",
        "data.amount.real",
        "[x for x in range(10)]",
        "lambda: 1",
        "points[0]",
        "2 ** 100000",
        "f'{points}'",
        "exec('1')",
        "floor(amount=1)",
        "points; points",
        "",
        "(" * 300 + ")" * 300,
        "x and " * 200 + "x",
    ],
)
def test_expressions_cannot_do_anything_but_calculate(expression: str) -> None:
    with pytest.raises(ExpressionError):
        parse(expression)


def test_expressions_report_missing_values_and_impossible_arithmetic() -> None:
    assert names_used("points == floor(data.amount / 10) and env.MODE == 'x'") == {
        "var.points",
        "data.amount",
        "env.MODE",
    }
    for expression, values in (
        ("points > 1", {}),
        ("1 / zero == 1", {"var.zero": 0}),
        ("amount + 1 == 2", {"var.amount": "about ten"}),
        ("amount + 1 == 2", {"var.amount": "3 of 10"}),  # two numbers: not guessed at
        ("flag + 1 == 2", {"var.flag": True}),
    ):
        with pytest.raises(ExpressionError):
            evaluate(expression, values)
    assert to_number("₹1,250.50") == Decimal("1250.50")
    assert to_number("-3") == Decimal(-3)


# --- text matching, pointers, values ------------------------------------------------------


def test_text_matching_rules() -> None:
    def match(spec: dict[str, Any], actual: Any) -> bool:
        model = TextMatch.model_validate(spec)
        return matches(model, model.text, actual)

    assert match({"equals": "Order  placed"}, " Order placed\n")  # white space as displayed
    assert not match({"equals": "order placed"}, "Order placed")
    assert match({"equals": "order placed", "ignore_case": True}, "Order placed")
    assert match({"contains": "placed"}, "Order placed") and not match(
        {"contains": "paid"}, "Order placed"
    )
    assert match({"matches": r"^Order #\d+$"}, "Order #42") and not match(
        {"matches": r"^\d+$"}, "Order #42"
    )
    assert match({"not_equals": "Pending"}, "Paid") and not match({"not_equals": "Paid"}, "Paid")
    assert not match({"equals": "x"}, None)
    assert match({"equals": "50"}, 50)


def test_url_comparison_subject_and_json_pointers() -> None:
    url = "https://shop.example/orders?page=2"
    assert url_subject(url, "/orders") == "/orders"
    assert url_subject(url, "/orders?page=2") == "/orders?page=2"
    assert url_subject(url, "https://shop.example/orders?page=2") == url
    document = {"items": [{"price": 5, "a/b": 1, "~": 2}], "none": None}
    assert json_pointer(document, "/items/0/price") == (True, 5)
    assert json_pointer(document, "/items/0/a~1b") == (True, 1)
    assert json_pointer(document, "/items/0/~0") == (True, 2)
    assert json_pointer(document, "/none") == (True, None)
    assert json_pointer(document, "") == (True, document)
    for missing in ("/items/1", "/items/x", "/nope", "/items/0/price/deeper"):
        assert json_pointer(document, missing) == (False, None)


def test_values_resolve_placeholders_and_know_which_are_secret() -> None:
    values = Values(
        env={"REGION": "in"},
        data={"amount": "505"},
        credentials={"customer": {"username": "demo", "password": "s3cret"}},
    )
    assert (
        values.render("{{env.REGION}}/{{data.amount}}/{{cred.customer.username}}") == "in/505/demo"
    )
    assert values.render_any({"k": ["{{data.amount}}", 1]}) == {"k": ["505", 1]}
    assert values.secrets == {"s3cret"}
    step = script(
        {"action": "fill", "locator": BUTTON, "value": "{{cred.customer.password}}"}, CHECK
    ).steps[0]
    plain = script(
        {"action": "fill", "locator": BUTTON, "value": "{{cred.customer.username}}"}, CHECK
    ).steps[0]
    assert values.uses_secret(step) and not values.uses_secret(plain)
    assert values.render("{{unique.email}}") == values.render("{{unique.email}}")
    assert values.render("{{unique.email}}").endswith("@example.com")
    assert values.render("{{unique.number}}").isdigit()


# --- verdicts -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("statuses", "verdict"),
    [
        (["passed", "passed"], "passed"),
        (["passed", "flaky"], "flaky"),
        (["passed", "failed", "error"], "failed"),
        (["passed", "error"], "error"),
        (["passed", "skipped"], "inconclusive"),  # not everything ran: never a pass
        (["passed", "blocked"], "inconclusive"),
        (["passed", "pending"], "inconclusive"),
        (["skipped"], "inconclusive"),
        ([], "inconclusive"),  # nothing ran, so nothing was verified
        (["flaky", "skipped"], "inconclusive"),
        (["failed", "skipped"], "failed"),
    ],
)
def test_execution_verdict(statuses: list[str], verdict: str) -> None:
    assert execution_verdict(statuses) == verdict


def test_totals_count_every_status() -> None:
    assert totals(["passed", "passed", "failed", "skipped"]) == {
        "total": 4, "passed": 2, "failed": 1, "error": 0, "flaky": 0, "skipped": 1, "blocked": 0, "needs_review": 0,
    }  # fmt: skip


# --- configuration and wire format --------------------------------------------------------


def test_run_configuration_defaults_and_limits() -> None:
    config = RunConfig()
    assert (config.browser, config.viewport.label, config.retry.max_attempts) == (
        "chromium",
        "1280x720",
        1,
    )
    assert config.evidence.model_dump() == {
        "screenshots": "on_failure",
        "video": "off",
        "trace": "off",
        "logs": "on_failure",
        "full_page": False,
    }
    for bad in (
        {"browser": "netscape"},
        {"viewport": {"width": 10, "height": 10}},
        {"timeouts": {"action_ms": 1}},
        {"timeouts": {"test_ms": 10**9}},
        {"retry": {"max_attempts": 50}},
        {"retry": {"retry_on": ["skipped"]}},
        {"evidence": {"video": "sometimes"}},
        {"headless": False},
    ):
        with pytest.raises(ValidationError):
            RunConfig.model_validate(bad)


def _plan(cases: int = 2, **config: Any) -> RunPlan:
    return RunPlan.model_validate(
        {
            "run_id": str(uuid.uuid4()),
            "config": config,
            "base_url": "https://shop.example",
            "allowed_origins": ["https://shop.example"],
            "allowed_actions": ["navigate", "read"],
            "cases": [
                {
                    "result_id": str(uuid.uuid4()),
                    "key": f"TC-{n}",
                    "title": "t",
                    "script": {"steps": [CHECK]},
                }
                for n in range(cases)
            ],
        }
    )


def test_run_time_budget_grows_with_the_plan_and_is_capped() -> None:
    small = run_budget_s(_plan(1), 3600)
    assert run_budget_s(_plan(4), 3600) > small > 120
    assert run_budget_s(_plan(4, retry={"max_attempts": 3}), 3600) > run_budget_s(_plan(4), 3600)
    assert run_budget_s(_plan(400), 3600) == 3600


# --- engine client ------------------------------------------------------------------------


def _client(handler: Any) -> EngineClient:
    return EngineClient(
        "http://engine:8902",
        "engine-token-0123456789abcdef",
        transport=httpx.MockTransport(handler),
    )


async def test_engine_client_streams_typed_events_and_sends_its_token() -> None:
    seen: list[httpx.Request] = []
    step = StepFinished(
        result_id="r", attempt=1, phase="main", position=1, action="navigate", status="passed",
        started_at="2026-01-01T00:00:00Z", finished_at="2026-01-01T00:00:01Z", duration_ms=1000,
    )  # fmt: skip
    lines = [step.model_dump_json(), "", RunFinished(status="completed").model_dump_json()]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="\n".join(lines) + "\n")

    events = [event async for event in _client(handler).run(_plan())]

    assert [type(e).__name__ for e in events] == ["StepFinished", "RunFinished"]
    assert seen[0].headers["authorization"] == "Bearer engine-token-0123456789abcdef"
    assert json.loads(seen[0].content)["cases"][0]["key"] == "TC-0"


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(429), "engine_busy"),
        (httpx.Response(500), "engine_unavailable"),
        (httpx.Response(200, text="not json\n"), "engine_protocol"),
        (httpx.Response(200, text='{"event": "made_up"}\n'), "engine_protocol"),
    ],
)
async def test_engine_client_reports_failures_as_engine_errors(
    response: httpx.Response, code: str
) -> None:
    with pytest.raises(EngineError) as raised:
        _ = [event async for event in _client(lambda _request: response).run(_plan())]
    assert raised.value.code == code


async def test_engine_client_unreachable_engine_and_missing_artifacts() -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to http://engine:8902")

    with pytest.raises(EngineError) as raised:
        _ = [event async for event in _client(refuse).run(_plan())]
    assert raised.value.code == "engine_unavailable" and "8902" not in raised.value.message
    with pytest.raises(EngineError) as missing:
        await _client(lambda _r: httpx.Response(404)).fetch_artifact("run", "a.png")
    assert missing.value.code == "artifact_missing"
    assert (
        await _client(lambda _r: httpx.Response(200, content=b"png")).fetch_artifact("run", "a.png")
        == b"png"
    )
    await _client(refuse).delete_run("run")  # best effort: never raises


# --- evidence store and test data ---------------------------------------------------------


def test_evidence_store_keeps_organizations_apart(tmp_path: Any) -> None:
    store = EvidenceStore(str(tmp_path))
    mine, theirs, execution = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    stored = store.put(mine, execution, "image/png", b"\x89PNG data")

    assert stored.key.startswith(f"{mine}/{execution}/") and stored.key.endswith(".png")
    assert stored.size_bytes == 9 and len(stored.sha256) == 64
    found = store.path(mine, stored.key)
    assert found is not None and found.read_bytes() == b"\x89PNG data"
    # Another organization cannot read it, by its key or by walking up the tree.
    assert store.path(theirs, stored.key) is None
    assert store.path(theirs, f"{theirs}/../{stored.key}") is None
    assert store.path(mine, f"{mine}/../../etc/passwd") is None
    assert store.path(mine, f"{mine}/{execution}/missing.png") is None
    store.delete_execution(mine, execution)
    assert store.path(mine, stored.key) is None


@pytest.mark.parametrize(
    ("value", "real"),
    [
        ("4242424242424242", False),  # a provider's published test number
        ("4111 1111 1111 1111", False),
        ("5105105105105100", False),
        ("4485275742308327", True),  # valid, and not on the list of test numbers
        ("4539 1488 0343 6467", True),
        ("card 4539-1488-0343-6467 exp 12/30", True),
        ("1234567812345678", False),  # fails the checksum: not a card number
        ("order 20261119000123", False),
        ("505", False),
    ],
)
def test_real_looking_card_numbers_are_recognised(value: str, real: bool) -> None:
    assert looks_like_real_card(value) is real


def test_every_script_survives_being_written_out_in_full_and_read_back() -> None:
    """The worker sends scripts to the engine as complete JSON, nulls included."""
    from tests.demo_scenarios import scenarios

    for scenario in scenarios():
        script = Script.model_validate(scenario.script)
        assert Script.model_validate_json(script.model_dump_json()) == script, scenario.key
    checks: list[dict[str, Any]] = [
        {"equals": None},  # compare with JSON null
        {"equals": 25},
        {"type": "number"},
        {"exists": False},
        {"text": {"contains": "x"}},
    ]
    for check in checks:
        step = {"action": "assert_api_json", "response": "r", "pointer": "/a", **check}
        script = Script.model_validate(
            {"steps": [{"action": "api_request", "url": "/x", "save_as": "r"}, step]}
        )
        again = Script.model_validate_json(script.model_dump_json())
        assert again == script, check
    wrongs: list[dict[str, Any]] = [
        {},
        {"equals": 1, "type": "number"},
        {"exists": True, "text": {"contains": "x"}},
    ]
    for wrong in wrongs:
        with pytest.raises(ValueError, match="exactly one"):
            Script.model_validate(
                {
                    "steps": [
                        {"action": "assert_api_json", "response": "r", "pointer": "/a", **wrong}
                    ]
                }
            )
