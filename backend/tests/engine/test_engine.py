"""The test engine in real browsers, against the demo application.

    make engine-test

Every supported action, locator strategy and assertion is exercised here, each
with the way it passes and the ways it does not. No model is involved, and
there is none to mock.
"""

import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from app.engine.protocol import RunPlan
from app.engine.runner import run_plan

pytestmark = pytest.mark.engine

DEMO = os.environ.get("DEMO_URL", "http://demo-app:8080")
PASSWORD = "demo-password"
ALL_ACTIONS = ["navigate", "read", "click", "type", "select", "upload", "api_read", "api_write"]
RECIPE = [
    {"action": "goto", "path": "/login"},
    {
        "action": "fill",
        "target": {"role": "textbox", "name": "Username"},
        "value": "{{cred.username}}",
    },
    {
        "action": "fill",
        "target": {"role": "textbox", "name": "Password"},
        "value": "{{cred.password}}",
    },
    {"action": "click", "target": {"role": "button", "name": "Sign in"}},
    {"action": "expect", "text": "Signed in as", "url_path": "/account"},
]
CUSTOMER = {
    "customer": {"fields": {"username": "demo", "password": PASSWORD}, "login_recipe": RECIPE}
}


def role(name: str, label: str | None = None, **more: Any) -> dict[str, Any]:
    return {"by": "role", "role": name, **({"name": label} if label else {}), **more}


def by(strategy: str, value: str, **more: Any) -> dict[str, Any]:
    return {"by": strategy, "value": value, **more}


def go(path: str) -> dict[str, Any]:
    return {"action": "navigate", "url": path}


SEES_PAGE: dict[str, Any] = {"action": "assert_visible", "locator": role("heading")}


@dataclass
class Run:
    events: list[dict[str, Any]]
    directory: Path

    def steps(self, result: int = 0, attempt: int | None = None) -> list[dict[str, Any]]:
        ids = list(
            dict.fromkeys(e["result_id"] for e in self.events if e["event"] == "case_started")
        )
        return [
            e
            for e in self.events
            if e["event"] == "step_finished"
            and e["result_id"] == ids[result]
            and (attempt is None or e["attempt"] == attempt)
        ]

    def cases(self) -> list[dict[str, Any]]:
        return [e for e in self.events if e["event"] == "case_finished" and e["final"]]

    @property
    def case(self) -> dict[str, Any]:
        return self.cases()[0]

    @property
    def last(self) -> dict[str, Any]:
        """The last step that was not skipped."""
        return [s for s in self.steps() if s["status"] != "skipped"][-1]

    def statuses(self, result: int = 0) -> list[str]:
        return [s["status"] for s in self.steps(result)]

    def text(self) -> str:
        return str(self.events)

    def files(self) -> list[str]:
        return sorted(p.name for p in self.directory.iterdir() if p.is_file())


async def run(
    *steps: dict[str, Any], scripts: list[dict[str, Any]] | None = None, **plan: Any
) -> Run:
    """Run one case made of `steps` (or several, given as `scripts`) and collect the events."""
    case_options: dict[str, Any] = {
        k: plan.pop(k) for k in ("cleanup", "login_as", "data_sets") if k in plan
    }
    data = plan.pop("data", {})
    scripts = scripts or [{"steps": list(steps), **case_options}]
    document = {
        "run_id": str(uuid.uuid4()),
        "config": plan.pop("config", {}),
        "base_url": DEMO,
        "allowed_origins": [DEMO],
        "allowed_actions": ALL_ACTIONS,
        "cases": [
            {
                "result_id": str(uuid.uuid4()),
                "key": f"TC-{n}",
                "title": f"case {n}",
                "script": s,
                "data": data,
            }
            for n, s in enumerate(scripts, start=1)
        ],
        **plan,
    }
    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    directory = Path(tempfile.mkdtemp(prefix="engine-test-"))
    await run_plan(RunPlan.model_validate(document), directory, emit)
    assert events[-1] == {
        "event": "run_finished",
        "status": "completed",
        "error_type": None,
        "error_message": None,
    }, events[-1]
    return Run(events, directory)


async def passes(*steps: dict[str, Any], **plan: Any) -> Run:
    result = await run(*steps, **plan)
    assert result.case["status"] == "passed", (result.case["failure_summary"], result.statuses())
    return result


async def fails(*steps: dict[str, Any], **plan: Any) -> Run:
    """The last step is an assertion that must not hold."""
    result = await run(*steps, **plan)
    assert (result.case["status"], result.case["failure_class"]) == ("failed", "assertion"), (
        result.case
    )
    assert (result.last["status"], result.last["error_type"]) == ("failed", "assertion")
    assert result.last["expected"] and result.last["actual"]
    return result


async def errors(error_type: str, *steps: dict[str, Any], **plan: Any) -> Run:
    """The last step cannot be carried out, for the given reason."""
    result = await run(*steps, **plan)
    assert result.case["status"] == "error", result.case
    assert (result.last["status"], result.last["error_type"]) == ("error", error_type), result.last
    return result


FAST = {"timeouts": {"action_ms": 1500}}

# --- actions ------------------------------------------------------------------------------


async def test_navigate_by_path_and_by_full_address() -> None:
    result = await passes(
        go("/"), go(f"{DEMO}/form"), {"action": "assert_title", "title": {"equals": "Order form"}}
    )
    assert result.steps()[0]["actual"] == {"url": f"{DEMO}/", "status": 200}
    assert result.steps()[0]["started_at"] <= result.steps()[0]["finished_at"]


async def test_navigating_outside_the_allowed_origins_is_refused_before_any_request() -> None:
    result = await errors("destination_not_allowed", go("/"), go("http://example.com/"), SEES_PAGE)
    assert result.case["failure_class"] == "network"
    assert result.statuses() == ["passed", "error", "skipped"]
    assert result.steps()[1]["page_url"] == f"{DEMO}/"  # the browser never left


async def test_a_redirect_out_of_the_allowed_origins_is_an_error() -> None:
    result = await run(go("/redirect?to=http://example.com/"), SEES_PAGE)
    assert result.case["status"] == "error"
    assert result.steps()[0]["error_type"] in ("destination_not_allowed", "navigation")


async def test_click_follows_a_link_and_wait_for_navigation_sees_the_new_address() -> None:
    result = await passes(
        go("/"),
        {"action": "click", "locator": role("link", "Order form")},
        {"action": "wait_for_navigation", "url": {"equals": "/form"}},
        {"action": "assert_url", "url": {"equals": "/form"}},
    )
    assert result.steps()[1]["locator"] == {
        "by": "role",
        "role": "link",
        "name": "Order form",
        "matched": 1,
    }


async def test_wait_for_navigation_times_out_when_the_address_never_comes() -> None:
    await errors(
        "timeout",
        go("/"),
        {"action": "wait_for_navigation", "url": {"equals": "/nowhere"}, "timeout_ms": 800},
        SEES_PAGE,
    )


async def test_fill_clear_and_read_back_an_input() -> None:
    nickname = by("placeholder", "Nickname")
    await passes(
        go("/widgets"),
        {"action": "assert_value", "locator": nickname, "value": {"equals": "guest"}},
        {"action": "fill", "locator": nickname, "value": "tester"},
        {"action": "assert_value", "locator": nickname, "value": {"equals": "tester"}},
        {"action": "clear", "locator": nickname},
        {"action": "assert_value", "locator": nickname, "value": {"equals": ""}},
    )


async def test_select_option_by_label_and_by_value() -> None:
    product = by("label", "Product")
    await passes(
        go("/form"),
        {"action": "select_option", "locator": product, "label": "Coffee"},
        {"action": "assert_value", "locator": product, "value": {"equals": "coffee"}},
        {"action": "select_option", "locator": product, "value": "tea"},
        {"action": "assert_value", "locator": product, "value": {"equals": "tea"}},
    )
    await errors(
        "timeout",
        go("/form"),
        {"action": "select_option", "locator": product, "label": "Cocoa", "timeout_ms": 800},
        SEES_PAGE,
    )


async def test_check_uncheck_and_radio_buttons() -> None:
    news, express, standard = (
        by("label", "Newsletter"),
        by("label", "Express"),
        by("label", "Standard"),
    )
    await passes(
        go("/widgets"),
        {"action": "assert_checked", "locator": news},
        {"action": "uncheck", "locator": news},
        {"action": "assert_checked", "locator": news, "checked": False},
        {"action": "check", "locator": news},
        {"action": "assert_checked", "locator": news},
        {"action": "assert_checked", "locator": standard},
        {"action": "select_radio", "locator": express},
        {"action": "assert_checked", "locator": express},
        {"action": "assert_checked", "locator": standard, "checked": False},
    )
    await errors(
        "action_failed", go("/widgets"), {"action": "select_radio", "locator": news}, SEES_PAGE
    )
    await fails(
        go("/widgets"), {"action": "assert_checked", "locator": express, **{"timeout_ms": 500}}
    )


async def test_upload_an_approved_file_and_refuse_any_other() -> None:
    receipt = by("label", "Receipt")
    files = [
        {"name": "receipt.txt", "content_type": "text/plain", "content_base64": "aGVsbG8gd29ybGQ="}
    ]
    result = await passes(
        go("/upload"),
        {"action": "upload_file", "locator": receipt, "file": "receipt.txt"},
        {"action": "click", "locator": role("button", "Upload")},
        {"action": "assert_text", "locator": role("status"), "text": {"contains": "(11 bytes)"}},
        files=files,
    )
    assert result.steps()[1]["actual"] == {"file": "receipt.txt"}
    await errors(
        "test_data",
        go("/upload"),
        {"action": "upload_file", "locator": receipt, "file": "/etc/passwd"},
        SEES_PAGE,
        files=files,
    )


async def test_wait_for_element_that_appears_late_or_goes_away() -> None:
    await passes(
        go("/widgets"),
        {"action": "wait_for_element", "locator": role("status"), "state": "visible"},
        {"action": "assert_text", "locator": role("status"), "text": {"equals": "Prices updated"}},
        {"action": "wait_for_element", "locator": by("text", "No such thing"), "state": "detached"},
    )
    result = await errors(
        "locator_not_found",
        go("/widgets"),
        {"action": "wait_for_element", "locator": by("text", "Never shown"), "timeout_ms": 600},
        SEES_PAGE,
    )
    assert 500 <= result.last["duration_ms"] < 3000


async def test_read_text_and_attribute_into_variables() -> None:
    result = await passes(
        go("/widgets"),
        {
            "action": "read_text",
            "locator": by("css", "p.price"),
            "save_as": "total",
            "as_number": True,
        },
        {"action": "read_text", "locator": by("test_id", "stock-level"), "save_as": "stock"},
        {
            "action": "read_attribute",
            "locator": by("test_id", "stock-level"),
            "attribute": "data-count",
            "save_as": "count",
        },
        {
            "action": "assert_expression",
            "expression": "total == 1250.50 and count == 7 and stock == '7 in stock'",
        },
        {"action": "fill", "locator": by("placeholder", "Nickname"), "value": "has {{var.count}}"},
        {
            "action": "assert_value",
            "locator": by("placeholder", "Nickname"),
            "value": {"equals": "has 7"},
        },
    )
    assert result.steps()[1]["actual"] == {"value": "1250.50", "saved_as": "total"}
    await errors(
        "action_failed",
        go("/widgets"),
        {
            "action": "read_text",
            "locator": by("test_id", "stock-level"),
            "save_as": "n",
            "as_number": True,
        },
        {"action": "read_text", "locator": role("heading"), "save_as": "n", "as_number": True},
        SEES_PAGE,
    )
    await errors(
        "action_failed",
        go("/widgets"),
        {
            "action": "read_attribute",
            "locator": role("heading"),
            "attribute": "data-nope",
            "save_as": "x",
        },
        SEES_PAGE,
    )


async def test_screenshot_step_produces_a_png() -> None:
    result = await passes(
        go("/"), {"action": "screenshot", "name": "home", "full_page": True}, SEES_PAGE
    )
    artifact = result.steps()[1]["artifact"]
    assert artifact["kind"] == "screenshot" and artifact["content_type"] == "image/png"
    assert (result.directory / artifact["name"]).read_bytes().startswith(b"\x89PNG")
    assert artifact in result.case["artifacts"]


# --- locators -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "locator",
    [
        role("button", "Pay now"),
        role("heading", "Widgets", exact=True),
        by("label", "Email address"),
        by("text", "7 in stock"),
        by("text", "in stock"),
        by("placeholder", "Nickname"),
        by("test_id", "stock-level"),
        by("css", "p.price"),
        role("button", "Save", within=role("region", "Billing")),
        by("css", "#late"),
        by("text", "Tea", exact=True, within=role("list", "Basket")),
    ],
)
async def test_every_locator_strategy_finds_its_element(locator: dict[str, Any]) -> None:
    result = await passes(go("/widgets"), {"action": "assert_exists", "locator": locator})
    assert result.last["locator"]["matched"] == 1


async def test_a_locator_matching_several_elements_is_an_error_not_a_guess() -> None:
    save = role("button", "Save")
    for step in (
        {"action": "click", "locator": save},
        {"action": "assert_exists", "locator": save, "timeout_ms": 500},
        {"action": "assert_visible", "locator": save, "timeout_ms": 500},
        {"action": "assert_enabled", "locator": save, "timeout_ms": 500},
        {
            "action": "assert_text",
            "locator": role("listitem"),
            "text": {"equals": "Tea"},
            "timeout_ms": 500,
        },
    ):
        result = await errors("locator_ambiguous", go("/widgets"), step, SEES_PAGE)
        assert result.case["failure_class"] == "locator"
        assert "elements match" in result.last["error_message"]


async def test_a_locator_matching_nothing_is_reported_as_such() -> None:
    missing = role("button", "Does not exist")
    for step in (
        {"action": "click", "locator": missing},
        {"action": "fill", "locator": missing, "value": "x"},
        {"action": "assert_enabled", "locator": missing},
        {"action": "assert_text", "locator": missing, "text": {"equals": "x"}},
    ):
        result = await errors("locator_not_found", go("/widgets"), step, SEES_PAGE, config=FAST)
        assert result.case["failure_class"] == "locator"


async def test_an_element_that_is_found_but_not_ready_times_out() -> None:
    result = await errors(
        "timeout",
        go("/widgets"),
        {"action": "click", "locator": role("button", "Pay now"), "timeout_ms": 700},
        SEES_PAGE,
    )
    assert result.case["failure_class"] == "timeout"


# --- assertions ---------------------------------------------------------------------------


async def test_visibility_and_existence() -> None:
    note, heading, items = by("css", "#secret-note"), role("heading", "Widgets"), role("listitem")
    await passes(
        go("/widgets"),
        {"action": "assert_visible", "locator": heading},
        {"action": "assert_hidden", "locator": note},
        {"action": "assert_exists", "locator": note},  # in the page, though not shown
        {"action": "assert_hidden", "locator": by("text", "Not on this page")},
        {"action": "assert_absent", "locator": by("text", "Not on this page")},
        {"action": "assert_count", "locator": items, "count": 3},
    )
    quick = {"timeout_ms": 500}
    for step in (
        {"action": "assert_visible", "locator": note},
        {"action": "assert_visible", "locator": by("text", "Not on this page")},
        {"action": "assert_hidden", "locator": heading},
        {"action": "assert_exists", "locator": by("text", "Not on this page")},
        {"action": "assert_absent", "locator": heading},
        {"action": "assert_count", "locator": items, "count": 4},
    ):
        await fails(go("/widgets"), {**step, **quick})


async def test_enabled_and_disabled_state() -> None:
    pay, heading = role("button", "Pay now"), by("label", "Email address")
    await passes(
        go("/widgets"),
        {"action": "assert_disabled", "locator": pay},
        {"action": "assert_enabled", "locator": heading},
    )
    await fails(go("/widgets"), {"action": "assert_enabled", "locator": pay, "timeout_ms": 500})
    await fails(
        go("/widgets"), {"action": "assert_disabled", "locator": heading, "timeout_ms": 500}
    )


@pytest.mark.parametrize(
    ("match", "holds"),
    [
        ({"equals": "7 in stock"}, True),
        ({"equals": "7  IN stock", "ignore_case": True}, True),
        ({"contains": "in stock"}, True),
        ({"matches": r"^\d+ in stock$"}, True),
        ({"not_equals": "0 in stock"}, True),
        ({"equals": "8 in stock"}, False),
        ({"equals": "7 IN STOCK"}, False),
        ({"contains": "sold out"}, False),
        ({"matches": r"^\d+ left$"}, False),
        ({"not_equals": "7 in stock"}, False),
    ],
)
async def test_text_comparisons(match: dict[str, Any], holds: bool) -> None:
    step = {
        "action": "assert_text",
        "locator": by("test_id", "stock-level"),
        "text": match,
        "timeout_ms": 500,
    }
    result = await (passes if holds else fails)(go("/widgets"), step)
    assert result.last["actual"] == {"text": "7 in stock"}


async def test_an_assertion_waits_for_the_page_instead_of_failing_early() -> None:
    result = await passes(
        go("/widgets"),
        {
            "action": "assert_text",
            "locator": by("css", "#late"),
            "text": {"equals": "Prices updated"},
        },
    )
    assert result.last["duration_ms"] >= 300  # the text arrives after 700 ms


async def test_url_and_title() -> None:
    await passes(
        go("/form?product=tea&quantity=2"),
        {"action": "assert_url", "url": {"equals": "/form"}},  # a plain path ignores the query
        {"action": "assert_url", "url": {"equals": "/form?product=tea&quantity=2"}},
        {"action": "assert_url", "url": {"equals": f"{DEMO}/form?product=tea&quantity=2"}},
        {"action": "assert_url", "url": {"matches": r"^/form$"}},
        {"action": "assert_title", "title": {"equals": "Order placed"}},
        {"action": "assert_title", "title": {"contains": "placed"}},
    )
    wrong_url = await fails(
        go("/form"), {"action": "assert_url", "url": {"equals": "/account"}, "timeout_ms": 500}
    )
    assert wrong_url.last["actual"] == {"url": f"{DEMO}/form"}
    await fails(
        go("/form"), {"action": "assert_title", "title": {"equals": "Checkout"}, "timeout_ms": 500}
    )


async def test_form_validation_messages() -> None:
    email = by("label", "Email address")
    submit = role("button", "Save", within=role("region", "Profile"))
    result = await passes(
        go("/widgets"),
        {"action": "click", "locator": submit},
        {"action": "assert_url", "url": {"equals": "/widgets"}},  # the browser refused to submit
        {"action": "assert_validation_message", "locator": email},
        {"action": "fill", "locator": email, "value": "not-an-address"},
        {"action": "assert_validation_message", "locator": email, "message": {"contains": "@"}},
        {"action": "fill", "locator": email, "value": "{{unique.email}}"},
        {"action": "click", "locator": submit},
        {
            "action": "assert_text",
            "locator": role("alert"),
            "text": {"matches": r"^Saved qa-[0-9a-f]+@example\.com$"},
        },
    )
    assert result.steps()[3]["actual"]["message"]
    # A valid field has no message: asserting one fails.
    await fails(
        go("/widgets"),
        {"action": "fill", "locator": email, "value": "someone@example.com"},
        {"action": "assert_validation_message", "locator": email, "timeout_ms": 500},
    )


async def test_api_status_and_response_data() -> None:
    await passes(
        go("/"),
        {"action": "api_request", "url": "/api/points?amount=505", "save_as": "points"},
        {"action": "assert_api_status", "response": "points", "status": 200},
        {"action": "assert_api_status", "response": "points", "status": [200, 204]},
        {"action": "assert_api_json", "response": "points", "pointer": "/points", "equals": 50},
        {
            "action": "assert_api_json",
            "response": "points",
            "pointer": "/tier",
            "equals": {"name": "silver"},
        },
        {
            "action": "assert_api_json",
            "response": "points",
            "pointer": "/tier/name",
            "text": {"matches": "^sil"},
        },
        {"action": "assert_api_json", "response": "points", "pointer": "/points", "type": "number"},
        {"action": "assert_api_json", "response": "points", "pointer": "/missing", "exists": False},
        {"action": "api_request", "url": "/api/broken", "save_as": "broken"},
        {"action": "assert_api_status", "response": "broken", "status": 500},
    )
    request = {"action": "api_request", "url": "/api/points?amount=505", "save_as": "points"}
    for check in (
        {"action": "assert_api_status", "response": "points", "status": 404},
        {"action": "assert_api_json", "response": "points", "pointer": "/points", "equals": 51},
        {"action": "assert_api_json", "response": "points", "pointer": "/points", "equals": "50"},
        {"action": "assert_api_json", "response": "points", "pointer": "/missing", "equals": 1},
        {"action": "assert_api_json", "response": "points", "pointer": "/points", "type": "string"},
        {"action": "assert_api_json", "response": "points", "pointer": "/tier", "exists": False},
    ):
        await fails(go("/"), request, check)
    html = await fails(
        go("/"),
        {"action": "api_request", "url": "/form", "save_as": "page"},
        {"action": "assert_api_json", "response": "page", "pointer": "/x", "exists": True},
    )
    assert html.last["actual"] == {"json": False, "status": 200}
    await errors(
        "test_data",
        go("/"),
        {"action": "assert_api_status", "response": "never_made", "status": 200},
    )


async def test_api_requests_can_write_share_the_session_and_stay_in_scope() -> None:
    result = await passes(
        {"action": "api_request", "url": "/api/whoami", "save_as": "me"},
        {"action": "assert_api_json", "response": "me", "pointer": "/signed_in", "equals": True},
        {
            "action": "api_request",
            "method": "POST",
            "url": "/api/orders",
            "json_body": {"item": "{{unique.text}}"},
            "save_as": "made",
        },
        {"action": "assert_api_status", "response": "made", "status": 201},
        {
            "action": "assert_api_json",
            "response": "made",
            "pointer": "/item",
            "text": {"contains": "QA test"},
        },
        login_as="customer",
        credentials=CUSTOMER,
    )
    assert result.steps()[0]["phase"] == "setup"
    anonymous = await passes(
        {"action": "api_request", "url": "/api/whoami", "save_as": "me"},
        {"action": "assert_api_status", "response": "me", "status": 401},
    )
    assert anonymous.case["status"] == "passed"
    await errors(
        "destination_not_allowed",
        {"action": "api_request", "url": "http://example.com/api", "save_as": "x"},
        SEES_PAGE,
    )


# --- business rules -----------------------------------------------------------------------

LOYALTY: list[dict[str, Any]] = [
    {"action": "fill", "locator": by("label", "Bill amount"), "value": "{{data.bill_amount}}"},
    {"action": "click", "locator": role("button", "Calculate points")},
    {
        "action": "read_text",
        "locator": by("test_id", "points"),
        "save_as": "points",
        "as_number": True,
    },
    {
        "action": "assert_expression",
        "description": "One point for every 10 spent, rounded down",
        "expression": "points == floor(number(data.bill_amount) / 10)",
    },
]


@pytest.mark.parametrize("amount", ["499", "500", "501", "0", "9.99", "1234.56"])
async def test_business_rule_holds_for_the_loyalty_calculation(amount: str) -> None:
    result = await passes(go("/loyalty"), *LOYALTY, data={"bill_amount": amount})
    assert result.last["actual"]["holds"] is True
    assert result.last["actual"]["values"]["data.bill_amount"] == amount


async def test_business_rule_catches_a_seeded_rounding_defect() -> None:
    """The defective variant rounds to the nearest point: 506 gives 51, not 50."""
    result = await fails(go("/loyalty?variant=rounding"), *LOYALTY, data={"bill_amount": "506"})
    assert result.last["actual"] == {
        "holds": False,
        "values": {"data.bill_amount": "506", "var.points": "51"},
    }
    assert "floor" in result.last["expected"]["rule"]
    # The same variant happens to be right at 500, which is why boundaries are tested.
    await passes(go("/loyalty?variant=rounding"), *LOYALTY, data={"bill_amount": "500"})


async def test_a_rule_that_cannot_be_evaluated_is_an_error_not_a_failure() -> None:
    await errors(
        "test_data", go("/"), {"action": "assert_expression", "expression": "missing_value > 1"}
    )
    await errors("test_data", go("/"), {"action": "assert_expression", "expression": "1 + 1"})


# --- how a case is judged -----------------------------------------------------------------


async def test_a_failed_assertion_stops_the_case_and_later_steps_are_skipped() -> None:
    result = await run(
        go("/widgets"),
        {"action": "assert_title", "title": {"equals": "Wrong"}, "timeout_ms": 300},
        {"action": "click", "locator": role("button", "Pay now")},
        SEES_PAGE,
    )
    assert result.statuses() == ["passed", "failed", "skipped", "skipped"]
    assert [s["position"] for s in result.steps()] == [1, 2, 3, 4]
    assert result.case["details"]["assertions_failed"] == 1


async def test_continue_on_failure_reports_several_problems_in_one_run() -> None:
    soft = {"timeout_ms": 300, "continue_on_failure": True}
    result = await run(
        go("/widgets"),
        {"action": "assert_title", "title": {"equals": "Wrong"}, **soft},
        {"action": "assert_count", "locator": role("listitem"), "count": 9, **soft},
        {"action": "assert_visible", "locator": role("heading", "Widgets")},
    )
    assert result.statuses() == ["passed", "failed", "failed", "passed"]
    assert result.case["status"] == "failed"  # a soft failure is still a failure
    assert result.case["details"]["assertions_failed"] == 2


async def test_a_case_in_which_no_assertion_ran_is_not_a_pass() -> None:
    result = await run(go("/"), {"action": "click", "locator": role("link", "Order form")})
    assert result.statuses() == ["passed", "passed"]
    assert (result.case["status"], result.case["failure_class"]) == ("error", "infrastructure")
    assert "nothing was verified" in result.case["failure_summary"]


async def test_environment_limits_are_enforced_by_the_engine_itself() -> None:
    click = {"action": "click", "locator": role("link", "Order form")}
    await errors("environment_read_only", go("/"), click, SEES_PAGE, read_only=True)
    await errors(
        "action_not_allowed", go("/"), click, SEES_PAGE, allowed_actions=["navigate", "read"]
    )
    await errors(
        "environment_read_only",
        go("/"),
        {
            "action": "api_request",
            "method": "POST",
            "url": "/api/orders",
            "json_body": {"item": "x"},
            "save_as": "r",
        },
        SEES_PAGE,
        read_only=True,
    )
    # Reading stays possible.
    await passes(
        go("/"),
        {"action": "api_request", "url": "/api/orders", "save_as": "r"},
        SEES_PAGE,
        read_only=True,
    )


# --- timeouts -----------------------------------------------------------------------------


async def test_navigation_timeout() -> None:
    result = await errors(
        "timeout", go("/slow?seconds=20"), SEES_PAGE, config={"timeouts": {"navigation_ms": 1500}}
    )
    assert result.last["duration_ms"] < 6000


async def test_case_timeout_stops_the_case_and_skips_the_rest() -> None:
    result = await run(
        go("/"),
        go("/slow?seconds=30"),
        SEES_PAGE,
        config={"timeouts": {"test_ms": 5000, "navigation_ms": 60000}},
    )
    assert result.statuses() == ["passed", "error", "skipped"]
    assert result.steps()[1]["error_type"] == "test_timeout"
    assert (result.case["status"], result.case["failure_class"]) == ("error", "timeout")
    assert result.steps()[1]["duration_ms"] < 7000
    assert result.case["duration_ms"] < 20000


# --- cleanup ------------------------------------------------------------------------------

MAKE_ORDER: list[dict[str, Any]] = [
    {
        "action": "api_request",
        "method": "POST",
        "url": "/api/orders",
        "json_body": {"item": "{{unique.id}}"},
        "save_as": "order",
    },
    {"action": "assert_api_status", "response": "order", "status": 201},
    {"action": "api_request", "url": "/api/orders", "save_as": "list"},
    {
        "action": "assert_api_json",
        "response": "list",
        "pointer": "/orders/0/item",
        "text": {"matches": "^qa-"},
    },
]


async def test_cleanup_runs_after_a_failure_and_removes_what_the_case_created() -> None:
    result = await run(
        go("/"),
        *MAKE_ORDER,
        {"action": "assert_title", "title": {"equals": "Wrong"}, "timeout_ms": 300},
        SEES_PAGE,
        cleanup=[
            {"action": "api_request", "url": "/api/orders", "save_as": "before"},
            {
                "action": "api_request",
                "method": "DELETE",
                "url": "/api/orders/1",
                "save_as": "gone",
                "description": "Remove the order",
            },
        ],
    )
    cleanup = [s for s in result.steps() if s["phase"] == "cleanup"]
    assert [s["status"] for s in cleanup] == ["passed", "passed"]
    assert cleanup[1]["actual"]["status"] in (204, 404)
    assert result.case["status"] == "failed"  # cleanup does not change what the test found
    assert result.case["details"]["cleanup"] == "ok"
    assert result.statuses()[-3] == "skipped"  # the step after the failure did not run; cleanup did


async def test_failing_cleanup_is_flagged_without_changing_the_verdict() -> None:
    result = await passes(
        go("/"),
        SEES_PAGE,
        cleanup=[
            {"action": "click", "locator": role("button", "No such button"), "timeout_ms": 300},
            {"action": "api_request", "url": "/api/orders", "save_as": "still_runs"},
        ],
    )
    cleanup = [s for s in result.steps() if s["phase"] == "cleanup"]
    assert [s["status"] for s in cleanup] == ["error", "passed"]  # every cleanup step is attempted
    assert result.case["details"]["cleanup"] == "failed"


# --- retries ------------------------------------------------------------------------------


def _flaky(key: str) -> list[dict[str, Any]]:
    return [
        go(f"/flaky?key={key}"),
        {"action": "assert_visible", "locator": by("text", "Ready"), "timeout_ms": 500},
    ]


async def test_a_case_that_passes_only_on_retry_is_flaky_not_passed() -> None:
    result = await run(
        *_flaky(uuid.uuid4().hex), config={"retry": {"max_attempts": 2, "retry_on": ["failed"]}}
    )
    attempts = [e for e in result.events if e["event"] == "case_finished"]
    assert [(a["attempt"], a["final"], a["status"]) for a in attempts] == [
        (1, False, "failed"),
        (2, True, "flaky"),
    ]
    assert [s["status"] for s in result.steps(attempt=1)] == ["passed", "failed"]
    assert [s["status"] for s in result.steps(attempt=2)] == ["passed", "passed"]


async def test_without_retries_or_for_other_outcomes_the_first_result_stands() -> None:
    once = await run(*_flaky(uuid.uuid4().hex))
    assert once.case["status"] == "failed" and once.case["attempt"] == 1
    # Retrying errors only: an assertion failure is not retried.
    errors_only = await run(
        *_flaky(uuid.uuid4().hex), config={"retry": {"max_attempts": 3, "retry_on": ["error"]}}
    )
    assert errors_only.case["status"] == "failed" and errors_only.case["attempt"] == 1
    # A failure that stays is reported after the last attempt.
    stays = await run(go("/"), {"action": "assert_title", "title": {"equals": "x"}, "timeout_ms": 200},
                      config={"retry": {"max_attempts": 2, "retry_on": ["failed"]}})  # fmt: skip
    assert (stays.case["status"], stays.case["attempt"]) == ("failed", 2)


# --- sign-in, isolation and secrets -------------------------------------------------------

ACCOUNT = [
    go("/account"),
    {"action": "assert_text", "locator": by("text", "Points balance"), "text": {"contains": "120"}},
]


async def test_sign_in_before_the_case_and_contexts_are_isolated_between_cases() -> None:
    result = await run(
        scripts=[
            {"login_as": "customer", "steps": ACCOUNT},
            # No sign-in: must not inherit the previous case's session.
            {"steps": [go("/account"), {"action": "assert_url", "url": {"equals": "/login"}}]},
        ],
        credentials=CUSTOMER,
    )
    assert [c["status"] for c in result.cases()] == ["passed", "passed"]
    login = result.steps(0)[0]
    assert (login["phase"], login["action"], login["status"], login["position"]) == (
        "setup",
        "login",
        "passed",
        1,
    )
    assert PASSWORD not in result.text()


async def test_failed_sign_in_is_an_error_and_the_case_does_not_run() -> None:
    wrong = {
        "customer": {
            "fields": {"username": "demo", "password": "wrong-password"},
            "login_recipe": RECIPE,
        }
    }
    result = await run(
        *ACCOUNT,
        login_as="customer",
        credentials=wrong,
        config={"timeouts": {"navigation_ms": 3000}},
    )
    assert result.statuses() == ["error"]
    assert (result.case["status"], result.case["failure_class"]) == ("error", "environment")
    assert "wrong-password" not in result.text()
    missing = await run(*ACCOUNT, login_as="nobody")
    assert missing.steps()[0]["error_type"] == "login_failed"


async def test_a_case_that_types_a_secret_itself_never_shows_it() -> None:
    result = await run(
        go("/login"),
        {
            "action": "fill",
            "locator": by("label", "Username"),
            "value": "{{cred.customer.username}}",
        },
        {
            "action": "fill",
            "locator": by("label", "Password"),
            "value": "{{cred.customer.password}}",
        },
        {"action": "screenshot", "name": "filled"},
        {"action": "click", "locator": role("button", "Sign in")},
        {
            "action": "assert_text",
            "locator": by("text", "Signed in as"),
            "text": {"contains": "nobody"},
            "timeout_ms": 500,
        },
        credentials=CUSTOMER,
        config={"evidence": {"screenshots": "always", "video": "always", "trace": "always"}},
    )
    assert result.case["status"] == "failed"
    assert PASSWORD not in result.text()
    assert result.steps()[2]["actual"] == {"value": "[REDACTED]"}
    assert result.steps()[1]["actual"] == {"value": "demo"}  # the user name is not a secret
    # Video and trace would record the typing, so they are not made for this case.
    assert result.case["details"]["evidence_notes"] == ["recordings_skipped_secret_input"]
    assert {a["kind"] for a in result.case["artifacts"]} == {"screenshot"}
    assert not any(name.endswith((".webm", ".zip")) for name in result.files())


# --- evidence -----------------------------------------------------------------------------

FAILING = [
    go("/widgets"),
    {"action": "assert_title", "title": {"equals": "Wrong"}, "timeout_ms": 300},
]
PASSING: list[dict[str, Any]] = [go("/widgets"), SEES_PAGE]


async def test_evidence_on_failure_only() -> None:
    config = {
        "evidence": {"screenshots": "on_failure", "video": "on_failure", "trace": "on_failure"}
    }
    failed = await run(*FAILING, config=config)
    kinds = sorted(a["kind"] for a in failed.case["artifacts"])
    assert kinds == ["screenshot", "trace", "video"]
    assert failed.steps()[1]["artifact"]["kind"] == "screenshot"  # taken at the failing step
    for artifact in failed.case["artifacts"]:
        path = failed.directory / artifact["name"]
        assert path.stat().st_size == artifact["size_bytes"] > 0
    assert (
        failed.directory / next(a["name"] for a in failed.case["artifacts"] if a["kind"] == "trace")
    ).read_bytes()[:2] == b"PK"

    passed = await run(*PASSING, config=config)
    assert passed.case["artifacts"] == [] and passed.files() == []


async def test_evidence_always_and_off() -> None:
    always = await run(
        *PASSING,
        config={"evidence": {"screenshots": "always", "video": "always", "trace": "always"}},
    )
    assert sorted(a["kind"] for a in always.case["artifacts"]) == [
        "screenshot",
        "screenshot",
        "trace",
        "video",
    ]
    off = await run(
        *FAILING, config={"evidence": {"screenshots": "off", "video": "off", "trace": "off"}}
    )
    assert off.case["status"] == "failed" and off.case["artifacts"] == [] and off.files() == []


async def test_browser_errors_and_blocked_requests_are_recorded_as_details() -> None:
    result = await passes(go("/dialog"), SEES_PAGE, go("/external"), SEES_PAGE)
    details = result.case["details"]
    assert "demo console error" in details["console_errors"]
    blocked = details["blocked_requests"]
    assert blocked == [
        {
            "url": "http://tracker.invalid/pixel.png",
            "method": "GET",
            "reason": "origin_not_allowlisted",
        }
    ]


async def test_read_only_environment_blocks_writes_made_by_the_page_itself() -> None:
    """A link is allowed to be followed; the form post it leads to is not sent."""
    result = await run(
        go("/login"),
        {"action": "assert_visible", "locator": role("button", "Sign in")},
        read_only=True,
        allowed_actions=["navigate", "read"],
    )
    assert result.case["status"] == "passed"
    assert result.case["details"]["blocked_requests"] == []


# --- configuration ------------------------------------------------------------------------


@pytest.mark.parametrize("browser", ["chromium", "firefox", "webkit"])
async def test_each_browser_runs_the_same_script(browser: str) -> None:
    result = await passes(
        go("/loyalty"), *LOYALTY, data={"bill_amount": "505"}, config={"browser": browser}
    )
    started = result.events[0]
    assert started["browser"] == browser and started["browser_version"]


async def test_viewport_size_is_applied() -> None:
    result = await passes(
        go("/"),
        {"action": "screenshot", "name": "small"},
        SEES_PAGE,
        config={"viewport": {"width": 375, "height": 667}},
    )
    data = (result.directory / result.steps()[1]["artifact"]["name"]).read_bytes()
    width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    assert (width, height) == (375, 667)


async def test_test_data_comes_from_the_environment_the_case_and_generated_values() -> None:
    nickname = by("placeholder", "Nickname")
    result = await passes(
        go("/widgets"),
        {
            "action": "fill",
            "locator": nickname,
            "value": "{{env.GREETING}} {{data.name}} {{unique.id}} {{unique.id}} {{unique.id_other}}",
        },
        {
            "action": "read_attribute",
            "locator": nickname,
            "attribute": "placeholder",
            "save_as": "hint",
        },
        {
            "action": "assert_value",
            "locator": nickname,
            "value": {"matches": r"^Hello Ada (qa-\w+) \1 qa-\w+$"},
        },
        env={"GREETING": "Hello"},
        data={"name": "Ada"},
    )
    typed = result.steps()[1]["actual"]["value"].split()
    assert typed[2] == typed[3] != typed[4]  # the same name gives the same value within a case
    again = await passes(
        go("/widgets"),
        {"action": "fill", "locator": nickname, "value": "{{unique.id}}"},
        {"action": "assert_value", "locator": nickname, "value": {"matches": "^qa-"}},
    )
    assert again.steps()[1]["actual"]["value"] != typed[2]  # and a new one in every run
    await errors(
        "test_data",
        go("/widgets"),
        {"action": "fill", "locator": nickname, "value": "{{data.unknown}}"},
        SEES_PAGE,
    )
