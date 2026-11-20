"""The demo scenarios, run by the engine in a real browser.

make engine-test
"""

import pytest

from tests.demo_scenarios import Scenario, scenarios
from tests.engine.test_engine import run

pytestmark = pytest.mark.engine
FAST = {"timeouts": {"action_ms": 2500}}


@pytest.mark.parametrize("scenario", scenarios(), ids=lambda s: s.key)
async def test_each_scenario_ends_as_it_should(scenario: Scenario) -> None:
    result = await run(*scenario.steps, config={**FAST, **scenario.config})
    assert result.case["status"] == scenario.expected, (
        scenario.key,
        result.case["failure_summary"],
        result.statuses(),
    )
    if scenario.expected == "passed":
        assert all(status == "passed" for status in result.statuses())
    else:
        # Something was recorded about why: a failing step with a type and a message.
        assert result.last["error_type"] and result.last["error_message"]


def test_the_set_covers_what_it_claims_and_is_not_all_passes() -> None:
    found = {s.key for s in scenarios()}
    assert found >= {
        "successful_login", "failed_login", "customer_registration", "form_validation",
        "product_creation", "invalid_input", "business_rule", "api_response_validation",
        "responsive_layout", "browser_navigation",
    }  # fmt: skip
    outcomes = {s.expected for s in scenarios()}
    assert outcomes == {"passed", "failed", "error"}
