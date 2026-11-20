"""Classification, verdicts, failure records and flaky detection. All rules, no model."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.agent.schemas.failure import FailureExplanation
from app.models.execution import EXECUTION_VERDICTS, FAILURE_CATEGORIES, RESULT_STATUSES
from app.services.failure_analysis import (
    SUGGESTION_LABEL,
    as_suggestion,
    facts_for_model,
    failure_record,
)
from app.testing.classification import classify, verification_summary
from app.testing.flakiness import RunRecord, analyse
from app.testing.verdicts import execution_verdict, totals

SERVER_ERROR = {"failed_requests": [{"method": "POST", "url": "/api/orders", "status": 500}]}
PAGE_ERROR = {"page_errors": ["TypeError: total is undefined"]}

# --- classification -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "error_type", "failure_class", "details", "expected"),
    [
        # What the test expects is not what the application did.
        ("failed", "assertion", "assertion", {}, "test_assertion_failure"),
        # The same, with the application visibly erroring: that is the application.
        ("failed", "assertion", "assertion", SERVER_ERROR, "application_failure"),
        ("failed", "assertion", "assertion", PAGE_ERROR, "application_failure"),
        # The test could not do its work. Not a finding about the application.
        ("error", "locator_not_found", "locator", {}, "test_automation_failure"),
        ("error", "locator_ambiguous", "locator", {}, "test_automation_failure"),
        ("error", "test_data", "test_data", {}, "test_automation_failure"),
        ("error", "action_failed", "action", {}, "test_automation_failure"),
        ("error", "destination_not_allowed", "navigation", {}, "test_automation_failure"),
        ("error", "environment_read_only", "action", {}, "test_automation_failure"),
        # ...unless the page itself broke, which explains the missing element.
        ("error", "locator_not_found", "locator", SERVER_ERROR, "application_failure"),
        # An ambiguous locator is the script's problem whatever else happened.
        ("error", "locator_ambiguous", "locator", SERVER_ERROR, "test_automation_failure"),
        # The browser, engine or worker.
        ("error", "internal", "infrastructure", {}, "browser_infrastructure_failure"),
        ("error", None, "infrastructure", {}, "browser_infrastructure_failure"),
        ("error", "internal", "infrastructure", SERVER_ERROR, "browser_infrastructure_failure"),
        # The network.
        ("error", "navigation", "navigation", {}, "network_failure"),
        ("error", "request_failed", "network", {}, "network_failure"),
        # Signing in.
        ("error", "login_failed", "login", {}, "authentication_failure"),
        ("error", "login_failed", "login", SERVER_ERROR, "authentication_failure"),
        # Time ran out: by itself that says nothing about who is at fault.
        ("error", "timeout", "timeout", {}, "inconclusive"),
        ("error", "test_timeout", "timeout", {}, "inconclusive"),
        ("error", "timeout", "timeout", SERVER_ERROR, "application_failure"),
        ("error", "something_new", None, {}, "inconclusive"),
        # Not run, or not to be relied on.
        ("skipped", None, None, {}, "inconclusive"),
        ("blocked", None, None, {}, "inconclusive"),
        ("needs_review", None, None, {}, "inconclusive"),
    ],
)
def test_every_kind_of_problem_gets_its_own_category(
    status: str,
    error_type: str | None,
    failure_class: str | None,
    details: dict[str, Any],
    expected: str,
) -> None:
    found = classify(
        status=status, error_type=error_type, failure_class=failure_class, details=details
    )
    assert found is not None and found.category == expected
    assert found.category in FAILURE_CATEGORIES
    assert found.basis and found.as_dict()["decided_by"] == "rules"


def test_only_two_things_count_as_evidence_about_the_application() -> None:
    """Of everything that can stop a test, only an assertion that did not hold and
    the application visibly breaking are filed against the application."""
    about_the_application = {"application_failure", "test_assertion_failure"}
    for error_type, failure_class in [
        ("internal", "infrastructure"), ("navigation", "navigation"), ("request_failed", "network"),
        ("login_failed", "login"), ("locator_not_found", "locator"), ("locator_ambiguous", "locator"),
        ("test_data", "test_data"), ("timeout", "timeout"), ("destination_not_allowed", "navigation"),
    ]:  # fmt: skip
        found = classify(
            status="error", error_type=error_type, failure_class=failure_class, details={}
        )
        assert found is not None and found.category not in about_the_application, error_type


def test_a_passing_result_has_no_category_and_a_4xx_or_console_noise_is_not_an_application_failure() -> (
    None
):
    assert classify(status="passed", error_type=None, failure_class=None) is None
    assert classify(status="flaky", error_type=None, failure_class=None) is None
    noise = {
        "failed_requests": [{"method": "GET", "url": "/favicon.ico", "status": 404}],
        "console_errors": ["Failed to load resource"],
    }
    found = classify(
        status="error", error_type="locator_not_found", failure_class="locator", details=noise
    )
    assert found is not None and found.category == "test_automation_failure"


def test_an_api_step_answered_with_a_server_error_points_at_the_application() -> None:
    found = classify(
        status="failed",
        error_type="assertion",
        failure_class="assertion",
        details={},
        actual={"status": 503},
    )
    assert found is not None and found.category == "application_failure"
    assert "503" in " ".join(found.basis)


def test_a_case_that_never_started_because_the_engine_was_down_is_infrastructure() -> None:
    found = classify(
        status="skipped", error_type=None, failure_class=None, execution_error="engine_unavailable"
    )
    assert found is not None and found.category == "browser_infrastructure_failure"


def test_the_verification_summary_counts_what_the_verdict_rests_on() -> None:
    steps = [
        {"action": "navigate", "status": "passed"},
        {"action": "assert_text", "status": "passed"},
        {"action": "assert_api_status", "status": "passed"},
        {"action": "assert_expression", "status": "failed"},
        {"action": "assert_visible", "status": "skipped"},
    ]
    assert verification_summary(steps) == {
        "assertions_evaluated": 3,
        "assertions_held": 2,
        "assertions_failed": 1,
        "by_kind": {"ui": 1, "api": 1, "business_rule": 1},
        "decided_by": "engine assertions",
    }


# --- verdicts -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("statuses", "verdict"),
    [
        (["passed", "passed"], "passed"),
        (["passed", "flaky"], "flaky"),
        (["passed", "needs_review"], "needs_review"),
        (["flaky", "needs_review"], "needs_review"),  # review outranks flaky
        (["needs_review", "skipped"], "inconclusive"),
        (["needs_review", "error"], "error"),
        (["needs_review", "failed", "error"], "failed"),
        ([], "inconclusive"),
    ],
)
def test_needs_review_and_flaky_are_never_reported_as_passed(
    statuses: list[str], verdict: str
) -> None:
    assert execution_verdict(statuses) == verdict and verdict in EXECUTION_VERDICTS


def test_only_all_passed_gives_passed() -> None:
    for status in RESULT_STATUSES:
        if status != "passed":
            assert execution_verdict(["passed", status]) != "passed", status
    counted = totals(["passed", "flaky", "needs_review"])
    assert (counted["passed"], counted["flaky"], counted["needs_review"]) == (1, 1, 1)


# --- the failure record and the suggestion ------------------------------------------------


def _step(position: int, action: str, status: str, **more: Any) -> Any:
    values = {
        "id": uuid.uuid4(), "attempt": 1, "position": position, "phase": "main", "action": action,
        "description": None, "status": status, "duration_ms": 5, "resolved_locator": None, "expected": None,
        "actual": None, "error_type": None, "error_message": None, "page_url": "https://app.test/loyalty",
    }  # fmt: skip
    return SimpleNamespace(**{**values, **more})


def _failed_result() -> tuple[Any, list[Any], list[Any]]:
    result = SimpleNamespace(
        id=uuid.uuid4(), status="failed", attempt_count=1, failure_class="assertion",
        failure_summary="expected 50, found 51", test_case_id=uuid.uuid4(),
        details={
            "console_errors": ["Uncaught (in promise)"],
            "failed_requests": [{"method": "GET", "url": "/api/points", "status": 500}],
            "password": "should-not-matter",
        },
    )  # fmt: skip
    steps = [
        _step(1, "navigate", "passed"),
        _step(2, "click", "passed"),
        _step(3, "assert_text", "failed", expected={"equals": "50"}, actual={"text": "51"},
              error_type="assertion", error_message="expected 50, found 51"),
        _step(4, "click", "skipped"),
        _step(5, "click", "error", phase="cleanup", error_type="locator_not_found"),
    ]  # fmt: skip
    evidence = [
        SimpleNamespace(
            id=uuid.uuid4(), result_id=result.id, kind="screenshot", execution_step_id=steps[2].id
        ),
        SimpleNamespace(id=uuid.uuid4(), result_id=result.id, kind="trace", execution_step_id=None),
        SimpleNamespace(
            id=uuid.uuid4(), result_id=uuid.uuid4(), kind="screenshot", execution_step_id=None
        ),
    ]
    return result, steps, evidence


def test_a_failure_record_holds_everything_recorded_about_the_failure() -> None:
    result, steps, evidence = _failed_result()
    record = failure_record(
        result,
        steps,
        evidence,
        case_key="TC-0001",
        title="Points",
        expected_result="50 points are shown",
    )

    assert record["case"] == {
        "key": "TC-0001",
        "title": "Points",
        "expected_result": "50 points are shown",
    }
    bad = record["failed_step"]
    assert (bad["position"], bad["action"], bad["expected"], bad["actual"]) == (
        3, "assert_text", {"equals": "50"}, {"text": "51"}
    )  # fmt: skip
    assert bad["browser_error"] == "expected 50, found 51" and bad["page_url"].endswith("/loyalty")
    assert record["console_errors"] == ["Uncaught (in promise)"]
    assert record["network_errors"]["failed_requests"][0]["status"] == 500
    assert record["screenshot_evidence_ids"] == [str(evidence[0].id)]
    # The log stops at the step that failed; later and cleanup steps are not the cause.
    assert [s["position"] for s in record["execution_log"]] == [1, 2, 3]
    # The category comes from the rules: an assertion failed while the server answered 500.
    assert record["classification"]["category"] == "application_failure"
    assert record["classification"]["decided_by"] == "rules"


def test_the_model_is_given_facts_without_identifiers_and_its_answer_is_stored_as_a_suggestion() -> (
    None
):
    result, steps, evidence = _failed_result()
    record = failure_record(result, steps, evidence, case_key="TC-0001")
    facts = facts_for_model("R1", record)
    assert facts["ref"] == "R1" and facts["category_by_rules"] == "application_failure"
    assert str(result.id) not in str(facts) and "screenshot_evidence_ids" not in facts
    assert "should-not-matter" not in str(facts)

    explanation = FailureExplanation(
        ref="R1", likely_cause="application_defect", summary="The page showed 51.",
        possible_root_cause="Possibly a rounding difference.", impact="Wrong points shown.",
        supporting_facts=["expected 50, found 51"], debugging_steps=[f"step {n}" for n in range(9)],
        suggested_test_cases=[{"title": f"t{n}", "why": "w"} for n in range(6)], confidence="medium",
    )  # fmt: skip
    stored = as_suggestion(explanation, category="application_failure", model="stand-in")
    assert stored["advisory"] is True and stored["label"] == SUGGESTION_LABEL
    assert "Not a confirmed root cause" in stored["label"]
    assert (
        stored["agrees_with_rules"] is True and stored["category_by_rules"] == "application_failure"
    )
    assert len(stored["debugging_steps"]) == 5 and len(stored["suggested_test_cases"]) == 3
    assert "ref" not in stored and stored["model"] == "stand-in"


@pytest.mark.parametrize(
    ("likely_cause", "category", "agrees"),
    [
        ("application_defect", "browser_infrastructure_failure", False),
        ("application_defect", "network_failure", False),
        ("application_defect", "test_automation_failure", False),
        ("environment", "authentication_failure", True),
        ("test_script", "test_automation_failure", True),
        ("unknown", "inconclusive", None),
    ],
)
def test_a_model_that_blames_the_application_for_an_infrastructure_error_is_shown_to_disagree_with_the_rules(
    likely_cause: str, category: str, agrees: bool | None
) -> None:
    explanation = FailureExplanation.model_validate(
        {
            "ref": "R1", "likely_cause": likely_cause, "summary": "s", "possible_root_cause": "p", "impact": "i",
            "supporting_facts": [], "debugging_steps": [], "suggested_test_cases": [], "confidence": "high",
        }
    )  # fmt: skip
    stored = as_suggestion(explanation, category=category, model="m")
    # The category shown is still the one the rules gave.
    assert stored["category_by_rules"] == category and stored["agrees_with_rules"] is agrees


# --- flaky detection ----------------------------------------------------------------------

START = datetime(2026, 1, 1, tzinfo=UTC)
V1, V2, STAGING, QA = "v1", "v2", "staging", "qa"


def runs(
    statuses: str, *, version: str = V1, environment: str = STAGING, offset: int = 0, **more: Any
) -> list[RunRecord]:
    """`P` passed, `F` failed, `E` error, `K` flaky (passed on retry), `R` needs review."""
    names = {
        "P": "passed",
        "F": "failed",
        "E": "error",
        "K": "flaky",
        "R": "needs_review",
        "S": "skipped",
    }
    return [
        RunRecord(
            finished_at=START + timedelta(hours=offset + n), status=names[letter], version_id=version,
            environment_id=environment, **(more if letter != "P" else {}),
        )
        for n, letter in enumerate(statuses)
    ]  # fmt: skip


def test_an_outcome_that_flips_back_and_forth_is_flaky() -> None:
    found = analyse(runs("PFPFP", failure_class="assertion"))
    assert found.is_flaky and found.flips == 4 and found.kinds == ["intermittent"]
    assert (found.runs, found.passed, found.not_passed, found.flake_rate) == (5, 3, 2, 0.4)
    assert "changed 4 times" in found.evidence[0]


@pytest.mark.parametrize(
    "history",
    [
        "PPPPP",  # steady
        "FFFFF",  # broken, not flaky
        "PPPFFF",  # something changed and it stayed changed: a regression
        "FFFPPP",  # fixed
        "PF",  # too few runs to say
        "SSSS",  # never ran
    ],
)
def test_steady_results_regressions_and_fixes_are_not_flaky(history: str) -> None:
    found = analyse(runs(history, failure_class="assertion"))
    assert not found.is_flaky and found.kinds == []


def test_a_change_of_version_or_environment_is_not_flakiness() -> None:
    # Failing on the old version and passing on the new one, alternating in time,
    # compares different tests; likewise two environments.
    by_version = [*runs("FFF", version=V1), *runs("PPP", version=V2, offset=10)]
    assert not analyse(by_version).is_flaky
    interleaved: list[RunRecord] = []
    for n in range(4):
        interleaved += runs("P", environment=STAGING, offset=2 * n)
        interleaved += runs("F", environment=QA, offset=2 * n + 1, failure_class="assertion")
    found = analyse(interleaved)
    assert not found.is_flaky and found.flips == 0


def test_a_pass_on_retry_is_flaky_and_never_counted_as_passed() -> None:
    found = analyse(runs("PPKP"))
    assert found.is_flaky and found.passed_on_retry == 1
    assert (found.passed, found.not_passed) == (3, 1)
    assert "passed only after a retry" in found.evidence[0]


@pytest.mark.parametrize(
    ("more", "kind"),
    [
        ({"failure_class": "timeout"}, "timing"),
        ({"failure_class": "locator"}, "unstable_locator"),
        ({"failure_category": "network_failure"}, "network"),
        ({"failure_class": "navigation"}, "network"),
        ({"failure_category": "browser_infrastructure_failure"}, "environment"),
        ({"failure_category": "authentication_failure"}, "environment"),
        ({"failure_class": "assertion"}, "intermittent"),
    ],
)
def test_the_kind_of_instability_is_told_apart(more: dict[str, Any], kind: str) -> None:
    assert analyse(runs("PEPEP", **more)).kinds == [kind]


def test_a_run_that_needed_a_replacement_locator_counts_as_an_unstable_locator() -> None:
    found = analyse(runs("PPRP", healed=True))
    assert found.is_flaky and found.kinds == ["unstable_locator"]
    assert "replacement locator" in found.evidence[-1]


def test_only_the_recent_window_is_judged() -> None:
    history = [*runs("PFPFPF", failure_class="assertion"), *runs("PPPPP", offset=10)]
    assert analyse(history).is_flaky
    assert not analyse(history, window=5).is_flaky
    assert analyse([]).runs == 0 and analyse([]).last_status is None
