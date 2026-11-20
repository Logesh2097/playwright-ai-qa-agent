"""Locator recovery in a real browser, against a page whose labels have changed.

make engine-test
"""

from typing import Any

import pytest

from tests.engine.test_engine import by, errors, go, passes, role, run

pytestmark = pytest.mark.engine

RECOVER = {"healing": {"mode": "recover"}, "timeouts": {"action_ms": 1500}}
PROPOSE = {"healing": {"mode": "propose"}, "timeouts": {"action_ms": 1500}}
OFF = {"timeouts": {"action_ms": 1500}}
# Locators are exact here: by default Playwright also accepts a name that merely
# contains the words, which would find most of these without any recovery.
PAGE = go("/healing")
SEES_PROFILE = {"action": "assert_visible", "locator": role("heading", "Your profile settings")}

# A script written when the form said "Email address" and "Save".
OLD_SCRIPT: list[dict[str, Any]] = [
    PAGE,
    {
        "action": "fill",
        "locator": by("label", "Email address", exact=True),
        "value": "ada@example.com",
    },
    {"action": "click", "locator": role("button", "Save", exact=True)},
    {
        "action": "assert_text",
        "locator": role("status"),
        "text": {"equals": "Saved ada@example.com"},
    },
]


def healing_of(result: Any, index: int) -> dict[str, Any]:
    return result.steps()[index]["healing"]  # type: ignore[no-any-return]


async def test_a_renamed_field_and_button_are_recovered_and_both_locators_are_recorded() -> None:
    result = await passes(*OLD_SCRIPT, config=RECOVER)

    fill, click = result.steps()[1], result.steps()[2]
    assert (fill["status"], click["status"]) == ("passed", "passed")
    assert healing_of(result, 1)["original"] == {
        "by": "label",
        "value": "Email address",
        "exact": True,
    }
    assert healing_of(result, 1)["replacement"] == {
        "by": "role",
        "role": "textbox",
        "name": "E-mail address",
        "exact": True,
    }
    assert (
        healing_of(result, 1)["verified"] is True and healing_of(result, 1)["decision"] == "recover"
    )
    assert healing_of(result, 2)["replacement"]["name"] == "Save changes"
    assert healing_of(result, 2)["confidence"] >= 0.8
    # The step records what was actually used.
    assert click["locator"] == {
        "by": "role",
        "role": "button",
        "name": "Save changes",
        "exact": True,
        "matched": 1,
    }
    assert (
        result.case["details"]["healed_steps"] == 2
        and result.case["details"]["heal_proposals"] == 0
    )
    # The assertion ran as written, with its own expected value, and held.
    assert result.steps()[3]["healing"] is None and result.steps()[3]["expected"] == {
        "equals": "Saved ada@example.com"
    }


async def test_without_healing_the_same_script_fails_at_the_first_missing_element() -> None:
    result = await errors("locator_not_found", *OLD_SCRIPT[:2], SEES_PROFILE, config=OFF)
    assert result.last["healing"] is None and result.case["details"]["healed_steps"] == 0


async def test_propose_mode_finds_the_replacement_but_does_not_use_it() -> None:
    result = await errors("locator_not_found", *OLD_SCRIPT[:2], SEES_PROFILE, config=PROPOSE)
    found = result.last["healing"]
    assert (found["decision"], found["verified"]) == ("propose", False)
    assert found["replacement"]["name"] == "E-mail address"
    assert found["held_back"] == "Automatic recovery is switched off for this run."
    assert result.case["details"]["heal_proposals"] == 1


async def test_recovery_never_changes_what_an_assertion_expects() -> None:
    """The heading is found under its new name; a wrong expectation still fails."""
    recovered = await passes(
        PAGE,
        {
            "action": "assert_text",
            "locator": role("heading", "Profile settings", exact=True),
            "text": {"equals": "Your profile settings"},
        },
        config=RECOVER,
    )
    assert healing_of(recovered, 1)["replacement"]["name"] == "Your profile settings"

    result = await run(
        PAGE,
        {
            "action": "assert_text",
            "locator": role("heading", "Profile settings", exact=True),
            "text": {"equals": "Billing settings"},
        },
        config=RECOVER,
    )
    step = result.steps()[1]
    # Found via the replacement, compared with the original expectation, which does not hold.
    # The step is not passed off as recovered: the original error stands.
    assert step["status"] == "error" and step["error_type"] == "locator_not_found"
    assert step["healing"]["verified"] is False and step["healing"]["decision"] == "propose"
    assert step["healing"]["held_back"] == "The step did not pass with the replacement either."
    assert result.case["status"] == "error" and result.case["details"]["healed_steps"] == 0


@pytest.mark.parametrize(
    ("old_name", "reason"),
    [
        ("Remove account", "not close enough"),  # two different buttons are about as similar
        (
            "Subscribe",
            "different or destructive",
        ),  # "Unsubscribe" looks alike and means the reverse
        ("Log in", "not close enough"),  # the page only has "Log out"
        ("Export", "almost as close"),  # "Export report" and "Export reports"
    ],
)
async def test_a_replacement_that_could_do_something_else_is_never_clicked(
    old_name: str, reason: str
) -> None:
    result = await errors(
        "locator_not_found",
        PAGE,
        {"action": "click", "locator": role("button", old_name, exact=True)},
        SEES_PROFILE,
        config=RECOVER,
    )
    found = result.last["healing"]
    assert found["verified"] is False and found["decision"] in ("propose", "none")
    if found["decision"] == "propose":
        assert reason in found["held_back"], found["held_back"]
    assert result.case["details"]["healed_steps"] == 0
    assert result.steps()[2]["status"] == "skipped"  # nothing was clicked, the case stopped


async def test_a_delete_button_is_not_chosen_for_a_step_that_did_not_ask_to_delete() -> None:
    result = await errors(
        "locator_not_found",
        PAGE,
        {"action": "click", "locator": role("button", "Account", exact=True)},
        SEES_PROFILE,
        config=RECOVER,
    )
    found = result.last["healing"]
    assert found["verified"] is False
    assert found["decision"] != "recover"


@pytest.mark.parametrize(
    "locator",
    [
        by("css", "button.save-btn"),  # no name to compare with
        by("test_id", "save-button"),
        role("button"),  # a role alone says nothing about which button
        role("slider", "Save changes"),  # right name, different kind of element
        role("button", "Completely unrelated wording"),
    ],
)
async def test_nothing_is_recovered_without_a_name_or_with_the_wrong_kind_of_element(
    locator: dict[str, Any],
) -> None:
    result = await run(
        PAGE,
        {"action": "click", "locator": locator, "timeout_ms": 800},
        SEES_PROFILE,
        config=RECOVER,
    )
    step = result.steps()[1]
    assert step["status"] == "error"
    assert step["healing"] is None or step["healing"]["decision"] == "none"
    assert result.case["details"]["healed_steps"] == 0


async def test_an_element_asserted_visible_that_is_missing_is_a_failed_assertion_not_a_recovery() -> (
    None
):
    result = await run(
        PAGE,
        {
            "action": "assert_visible",
            "locator": role("heading", "Profile settings", exact=True),
            "timeout_ms": 500,
        },
        config=RECOVER,
    )
    assert result.steps()[1]["status"] == "failed" and result.steps()[1]["healing"] is None


async def test_only_missing_elements_are_healed_not_failed_assertions_or_ambiguous_locators() -> (
    None
):
    wrong_text = await run(
        PAGE,
        {
            "action": "assert_text",
            "locator": role("heading", "Your profile settings"),
            "text": {"equals": "Something else"},
            "timeout_ms": 500,
        },
        config=RECOVER,
    )
    assert wrong_text.steps()[1]["status"] == "failed" and wrong_text.steps()[1]["healing"] is None
    ambiguous = await run(
        PAGE, {"action": "click", "locator": role("button", "Export")}, SEES_PROFILE, config=RECOVER
    )
    assert ambiguous.steps()[1]["error_type"] in ("locator_ambiguous", "locator_not_found")
    assert ambiguous.case["details"]["healed_steps"] == 0


async def test_the_number_of_recovered_steps_per_case_is_limited() -> None:
    limited = {"healing": {"mode": "recover", "max_per_case": 1}, "timeouts": {"action_ms": 1500}}
    result = await run(*OLD_SCRIPT, config=limited)

    assert result.steps()[1]["status"] == "passed" and healing_of(result, 1)["verified"] is True
    second = result.steps()[2]
    assert second["status"] == "error" and second["healing"]["verified"] is False
    assert "needs a person" in second["healing"]["held_back"]
    assert result.case["details"] | {} and result.case["details"]["healed_steps"] == 1
    assert result.case["status"] == "error"


async def test_a_step_is_tried_with_a_replacement_once_and_cleanup_is_not_healed() -> None:
    result = await passes(
        PAGE,
        SEES_PROFILE,
        cleanup=[
            {"action": "click", "locator": role("button", "Save", exact=True), "timeout_ms": 500}
        ],
        config=RECOVER,
    )
    cleanup = next(s for s in result.steps() if s["phase"] == "cleanup")
    assert cleanup["status"] == "error" and cleanup["healing"] is None
    assert (
        result.case["details"]["cleanup"] == "failed"
        and result.case["details"]["healed_steps"] == 0
    )


async def test_recovery_inside_a_named_region_stays_inside_it() -> None:
    inside = role("button", "Save", exact=True, within=role("region", "Profile"))
    result = await passes(
        PAGE, {"action": "click", "locator": inside}, SEES_PROFILE, config=RECOVER
    )
    replacement = healing_of(result, 1)["replacement"]
    assert replacement["name"] == "Save changes" and replacement["within"] == {
        "by": "role",
        "role": "region",
        "name": "Profile",
    }

    elsewhere = role("button", "Save", exact=True, within=role("region", "Danger zone"))
    missed = await run(
        PAGE, {"action": "click", "locator": elsewhere}, SEES_PROFILE, config=RECOVER
    )
    assert missed.steps()[1]["status"] == "error" and missed.case["details"]["healed_steps"] == 0


# --- names that look alike and are not the same thing -------------------------------------


@pytest.mark.parametrize(
    ("wanted", "found", "reason"),
    [
        ("Pay 10.00", "Pay 100.00", "different numbers"),
        ("Order 12", "Order 13", "different numbers"),
        ("Report 2025", "Report 2026", "different numbers"),
        ("Message Anna", "Message Hanna", "was replaced by"),
        ("Delete draft", "Delete drafts folder", None),  # words added only
        ("Ship to Berlin", "Ship to Bern", "was replaced by"),
    ],
)
def test_a_different_number_or_a_replaced_word_is_a_different_target(
    wanted: str, found: str, reason: str | None
) -> None:
    from app.engine.healing import changes_meaning

    answer = changes_meaning(wanted, found)
    assert (reason in answer) if reason and answer else (answer is None) is (reason is None), answer


@pytest.mark.parametrize(
    ("wanted", "found"),
    [
        ("Save", "Save changes"),
        ("Email address", "E-mail address"),
        ("Export report", "Export reports"),
        ("Terms & Conditions", "Terms and Conditions"),
        ("Add to shopping cart", "Add to cart"),
        ("Download invoice PDF", "Download PDF invoice"),
        ("sign in", "Sign In"),
        ("Category", "Categories"),
    ],
)
def test_added_dropped_reordered_and_respelled_words_are_the_same_target(
    wanted: str, found: str
) -> None:
    from app.engine.healing import changes_meaning

    assert changes_meaning(wanted, found) is None
