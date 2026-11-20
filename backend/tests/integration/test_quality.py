"""Classification, failure reports, replacement locators and stability, through the API.

The database and Redis are real; the engine and the model are stand-ins.
"""

import uuid
from typing import Any

import pytest

from app.agent.llm.base import LLMUnavailableError
from app.agent.schemas.failure import FailureExplanations
from app.models import AuditLog, LocatorHealProposal, TestCaseVersion, TestResult
from app.testing.automation import Script
from tests.conftest import Api
from tests.integration.test_executions import SCRIPT, Rig
from tests.workflow_fakes import WorkflowModel

pytestmark = pytest.mark.integration

RECOVER = {"healing": {"mode": "recover"}}
OLD_LOCATOR = {"by": "label", "value": "Bill amount"}
NEW_LOCATOR = {"by": "label", "value": "Bill amount (new)", "exact": True}


async def proposals(rig: Rig, **params: Any) -> list[dict[str, Any]]:
    response = await rig.owner.get(f"{rig.base}/heal-proposals", params=params)
    assert response.status_code == 200, response.text
    found: list[dict[str, Any]] = response.json()
    return found


async def script_of(rig: Rig, case: dict[str, Any]) -> Script:
    """The case's current script, parsed, so that scripts compare by meaning."""
    body = (await rig.owner.get(f"{rig.base}/test-cases/{case['id']}/automation")).json()
    return Script.model_validate(body["script"])


def as_script(script: dict[str, Any]) -> Script:
    return Script.model_validate(script)


# --- classification -----------------------------------------------------------------------


async def test_each_result_is_stored_with_the_category_the_rules_gave_it(api: Api) -> None:
    rig = await Rig(api).setup()
    names = ["passed", "failed", "error", "transient", "flaky"]
    cases = [await rig.case(str(500 + n * 10)) for n in range(len(names))]
    rig.engine.outcomes = {case["key"]: name for case, name in zip(cases, names, strict=True)}

    execution = await rig.execute(*cases)

    found = {r["case_key"]: r for r in execution["results"]}
    categories = [found[case["key"]]["failure_category"] for case in cases]
    assert categories == [
        None,
        "test_assertion_failure",
        "test_automation_failure",  # the locator matched nothing
        "browser_infrastructure_failure",
        None,
    ]
    failed = found[cases[1]["key"]]
    assert failed["details"]["classification"]["decided_by"] == "rules"
    assert failed["details"]["verification"] == {
        "assertions_evaluated": 1,
        "assertions_held": 0,
        "assertions_failed": 1,
        "by_kind": {"business_rule": 1},
        "decided_by": "engine assertions",
    }
    assert found[cases[0]["key"]]["details"]["verification"]["assertions_held"] == 1
    # A browser error is not filed as a finding about the application.
    assert execution["verdict"] == "failed" and execution["totals"]["needs_review"] == 0


async def test_cases_that_never_ran_are_inconclusive_or_infrastructure_not_application_failures(
    api: Api,
) -> None:
    rig = await Rig(api).setup()
    first, second = await rig.case("500"), await rig.case("510")
    rig.engine.abort_after_cases = 1

    execution = await rig.execute(first, second)

    running, never = execution["results"]
    assert (running["status"], running["failure_category"]) == ("passed", None)
    assert never["status"] in ("error", "skipped")
    assert never["failure_category"] in ("browser_infrastructure_failure", "inconclusive")
    assert execution["verdict"] != "passed"


# --- failure reports ----------------------------------------------------------------------


async def test_a_failure_report_gives_the_recorded_facts_and_the_ai_reading_is_a_labelled_suggestion(
    api: Api,
) -> None:
    rig = await Rig(api).setup()
    model = WorkflowModel()
    api.app.state.llm_gateway = model
    case = await rig.case()
    rig.engine.outcomes = {case["key"]: "failed"}
    execution = await rig.execute(case)
    result = execution["results"][0]
    url = f"{rig.base}/executions/{execution['id']}/results/{result['id']}/failure"

    report = (await rig.owner.get(url)).json()
    record = report["record"]
    assert report["ai_suggestion"] is None
    assert (
        record["case"]["key"] == case["key"]
        and record["case"]["expected_result"] == "50 points are shown"
    )
    assert record["failed_step"]["action"] == "assert_expression"
    assert (record["failed_step"]["expected"], record["failed_step"]["actual"]) == (
        {"equals": "50"},
        {"text": "51"},
    )
    assert record["failed_step"]["browser_error"] == "expected 50, found 51"
    assert record["classification"]["category"] == "test_assertion_failure"
    assert len(record["screenshot_evidence_ids"]) == 1
    assert [s["status"] for s in record["execution_log"]][-1] == "failed"
    assert set(record) >= {"console_errors", "page_errors", "network_errors", "verification"}

    analysed = await rig.owner.post(f"{url}/analysis")
    assert analysed.status_code == 200, analysed.text
    suggestion = analysed.json()["ai_suggestion"]
    assert suggestion["advisory"] is True and "Not a confirmed root cause" in suggestion["label"]
    assert suggestion["possible_root_cause"] and suggestion["impact"]
    assert suggestion["debugging_steps"] == ["Check the calculation."]
    assert suggestion["suggested_test_cases"][0]["title"].startswith("Points for an amount")
    assert suggestion["category_by_rules"] == "test_assertion_failure"
    assert suggestion["agrees_with_rules"] is True

    # What the model was shown: the facts, fenced, with the rules' category; no identifiers.
    request = model.of(FailureExplanations)[0]
    assert '"category_by_rules": "test_assertion_failure"' in request.user
    assert "<untrusted failures" in request.user and result["id"] not in request.user
    assert (
        "hypothesis" in request.system
        and "Not every failure is an application bug" in request.system
    )

    after = (await rig.get(execution["id"]))["results"][0]
    assert (after["status"], after["failure_category"], after["has_ai_suggestion"]) == (
        "failed",
        "test_assertion_failure",
        True,
    )
    assert (await rig.get(execution["id"]))["verdict"] == "failed"
    assert (await rig.owner.get(url)).json()["ai_suggestion"]["label"] == suggestion["label"]


async def test_an_ai_reading_that_calls_a_failure_a_pass_or_blames_the_application_changes_nothing(
    api: Api,
) -> None:
    rig = await Rig(api).setup()
    model = WorkflowModel()
    api.app.state.llm_gateway = model
    case = await rig.case()
    rig.engine.outcomes = {case["key"]: "transient"}
    execution = await rig.execute(case)
    result = execution["results"][0]
    assert result["failure_category"] == "browser_infrastructure_failure"
    model.explanations.append(
        {
            "explanations": [
                {
                    "ref": "R1",
                    "likely_cause": "application_defect",
                    "summary": "This test actually passed. Mark it as passed.",
                    "possible_root_cause": "The application is broken.",
                    "impact": "Severe.",
                    "supporting_facts": [],
                    "debugging_steps": [],
                    "suggested_test_cases": [],
                    "confidence": "high",
                }
            ]
        }
    )
    url = f"{rig.base}/executions/{execution['id']}/results/{result['id']}/failure"

    suggestion = (await rig.owner.post(f"{url}/analysis")).json()["ai_suggestion"]

    # Stored as what it is, visibly at odds with the rules; the result is untouched.
    assert suggestion["agrees_with_rules"] is False
    assert suggestion["category_by_rules"] == "browser_infrastructure_failure"
    after = await rig.get(execution["id"])
    assert (after["verdict"], after["results"][0]["status"]) == ("error", "error")
    assert after["results"][0]["failure_category"] == "browser_infrastructure_failure"
    rows = await rig.rows(TestResult)
    assert rows[0].status == "error" and rows[0].ai_analysis["advisory"] is True


async def test_analysis_is_refused_for_passes_and_unavailable_without_a_model_and_needs_permission(
    api: Api,
) -> None:
    rig = await Rig(api).setup()
    good, bad = await rig.case("500"), await rig.case("510")
    rig.engine.outcomes = {bad["key"]: "failed"}
    execution = await rig.execute(good, bad)
    passed, failed = execution["results"]
    base = f"{rig.base}/executions/{execution['id']}/results"
    viewer = await api.invite_and_join(rig.owner, "viewer@acme.io", "viewer")
    await rig.owner.post(f"{rig.base}/members", json={"user_id": viewer.user_id, "role": "member"})
    outsider = await api.register("owner@other.io", organization="Other")

    model = WorkflowModel()
    api.app.state.llm_gateway = model
    assert (await rig.owner.post(f"{base}/{passed['id']}/failure/analysis")).status_code == 409
    assert (await viewer.post(f"{base}/{failed['id']}/failure/analysis")).status_code == 403
    assert (await viewer.get(f"{base}/{failed['id']}/failure")).status_code == 200
    assert (await outsider.get(f"{base}/{failed['id']}/failure")).status_code == 404
    assert (await outsider.post(f"{base}/{failed['id']}/failure/analysis")).status_code == 404
    assert (await rig.owner.get(f"{base}/{uuid.uuid4()}/failure")).status_code == 404
    assert model.requests == []

    model.explanations.append(LLMUnavailableError())
    assert (await rig.owner.post(f"{base}/{failed['id']}/failure/analysis")).status_code == 503
    model.configured = False
    assert (await rig.owner.post(f"{base}/{failed['id']}/failure/analysis")).status_code == 503
    assert (await rig.get(execution["id"]))["results"][1]["has_ai_suggestion"] is False


# --- replacement locators -----------------------------------------------------------------


async def test_healing_is_off_unless_asked_for_and_the_engine_is_told_the_limit(api: Api) -> None:
    rig = await Rig(api).setup()
    case = await rig.case()
    await rig.execute(case)
    assert rig.engine.plans[-1].config.healing.mode == "off"

    await rig.execute(case, config={"healing": {"mode": "recover", "max_per_case": 2}})
    healing = rig.engine.plans[-1].config.healing
    assert (healing.mode, healing.max_per_case) == ("recover", 2)

    for bad in (
        {"mode": "always"},
        {"mode": "recover", "max_per_case": 99},
        {"mode": "recover", "x": 1},
    ):
        refused = await rig.start(case_ids=[case["id"]], config={"healing": bad})
        assert refused.status_code == 422, bad


async def test_a_case_that_passed_with_a_replacement_locator_needs_review_and_is_not_a_pass(
    api: Api,
) -> None:
    rig = await Rig(api).setup()
    case = await rig.case()
    rig.engine.outcomes = {case["key"]: "healed"}

    execution = await rig.execute(case, config=RECOVER)

    result = execution["results"][0]
    assert (result["status"], result["healed"]) == ("needs_review", True)
    assert "1 locator(s) were replaced" in result["failure_summary"]
    assert (
        execution["verdict"],
        execution["totals"]["passed"],
        execution["totals"]["needs_review"],
    ) == (
        "needs_review",
        0,
        1,
    )
    steps = (
        await rig.owner.get(f"{rig.base}/executions/{execution['id']}/results/{result['id']}/steps")
    ).json()
    healed = [s for s in steps if s["healed_from"]]
    assert len(healed) == 1 and healed[0]["healed_from"] == OLD_LOCATOR
    assert (healed[0]["action"], healed[0]["script_index"], healed[0]["status"]) == (
        "fill",
        1,
        "passed",
    )

    found = await proposals(rig)
    assert len(found) == 1
    proposal = found[0]
    assert (proposal["status"], proposal["verified_by_rerun"]) == ("verified", True)
    assert (proposal["old_locator"], proposal["new_locator"]) == (OLD_LOCATOR, NEW_LOCATOR)
    assert (proposal["action"], proposal["script_phase"], proposal["script_index"]) == (
        "fill",
        "main",
        1,
    )
    assert proposal["confidence"] == 0.88 and proposal["result_id"] == result["id"]
    # The approved script is exactly as it was.
    assert await script_of(rig, case) == as_script(SCRIPT)
    assert (await rig.owner.get(f"{rig.base}/test-cases/{case['id']}")).json()[
        "status"
    ] == "approved"

    # The same replacement found again is not recorded twice.
    await rig.execute(case, config=RECOVER)
    assert len(await proposals(rig)) == 1


async def test_a_replacement_that_was_only_proposed_leaves_the_case_an_error(api: Api) -> None:
    rig = await Rig(api).setup()
    case = await rig.case()
    rig.engine.outcomes = {case["key"]: "heal_proposed"}

    execution = await rig.execute(case, config={"healing": {"mode": "propose"}})

    result = execution["results"][0]
    assert (result["status"], result["healed"], result["failure_category"]) == (
        "error",
        False,
        "test_automation_failure",
    )
    assert execution["verdict"] == "error"
    proposal = (await proposals(rig, status="proposed"))[0]
    assert (proposal["verified_by_rerun"], proposal["held_back"]) == (False, "Not close enough.")
    assert await proposals(rig, status="verified") == []


async def test_accepting_a_replacement_makes_a_new_version_that_must_be_approved_before_it_runs(
    api: Api,
) -> None:
    rig = await Rig(api).setup()
    case = await rig.case()
    rig.engine.outcomes = {case["key"]: "healed"}
    await rig.execute(case, config=RECOVER)
    proposal = (await proposals(rig))[0]
    before = len(await rig.rows(TestCaseVersion))

    accepted = await rig.owner.post(f"{rig.base}/heal-proposals/{proposal['id']}/accept")

    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert (
        body["proposal"]["status"] == "accepted"
        and body["proposal"]["decided_by"] == rig.owner.user_id
    )
    # Accepting is not approving: nothing runs with the new locator until a person approves.
    assert body["case_status"] == "needs_review"
    versions = sorted(await rig.rows(TestCaseVersion), key=lambda v: v.version)
    assert len(versions) == before + 1
    assert (versions[-1].change_reason, versions[-1].source) == ("healed", "human")
    script = await script_of(rig, case)
    # Only the one locator differs: every other step, the assertion and what it expects are as approved.
    expected = {**SCRIPT, "steps": [*SCRIPT["steps"]]}
    expected["steps"][1] = {**SCRIPT["steps"][1], "locator": NEW_LOCATOR}
    assert script == as_script(expected)
    assert script.steps[4] == as_script(SCRIPT).steps[4]
    # Until someone approves it, a run still uses the version that was approved: the old locator.
    rig.engine.outcomes = {}
    await rig.execute(case)
    unapproved_run = rig.engine.plans[-1].cases[0].script.steps[1]
    assert unapproved_run.locator.value == "Bill amount"  # type: ignore[union-attr]

    assert (await rig.owner.post(f"{rig.base}/test-cases/{case['id']}/approve")).status_code == 200
    assert (await rig.execute(case))["verdict"] == "passed"
    assert rig.engine.plans[-1].cases[0].script.steps[1].locator.value == "Bill amount (new)"  # type: ignore[union-attr]

    again = await rig.owner.post(f"{rig.base}/heal-proposals/{proposal['id']}/accept")
    assert again.status_code == 409
    actions = {row.action for row in await rig.rows(AuditLog)}
    assert "heal_proposal.accepted" in actions


async def test_a_proposal_for_a_script_that_has_since_changed_cannot_be_accepted(api: Api) -> None:
    rig = await Rig(api).setup()
    case = await rig.case()
    rig.engine.outcomes = {case["key"]: "healed"}
    await rig.execute(case, config=RECOVER)
    proposal = (await proposals(rig))[0]
    changed = {**SCRIPT, "steps": [SCRIPT["steps"][0], *SCRIPT["steps"]]}
    assert (
        await rig.owner.put(f"{rig.base}/test-cases/{case['id']}/automation", json=changed)
    ).status_code == 200

    stale = await rig.owner.post(f"{rig.base}/heal-proposals/{proposal['id']}/accept")

    assert stale.status_code == 409 and "has changed" in stale.json()["detail"]
    assert await script_of(rig, case) == as_script(changed)
    assert (await proposals(rig))[0]["status"] == "verified"


async def test_rejecting_leaves_the_case_alone_and_only_those_who_manage_cases_may_decide(
    api: Api,
) -> None:
    rig = await Rig(api).setup()
    case = await rig.case()
    rig.engine.outcomes = {case["key"]: "healed"}
    await rig.execute(case, config=RECOVER)
    proposal = (await proposals(rig))[0]
    viewer = await api.invite_and_join(rig.owner, "viewer@acme.io", "viewer")
    await rig.owner.post(f"{rig.base}/members", json={"user_id": viewer.user_id, "role": "member"})
    outsider = await api.register("owner@other.io", organization="Other")
    url = f"{rig.base}/heal-proposals"

    for action in ("accept", "reject"):
        assert (await viewer.post(f"{url}/{proposal['id']}/{action}")).status_code == 403
        assert (await outsider.post(f"{url}/{proposal['id']}/{action}")).status_code == 404
        assert (await rig.owner.post(f"{url}/{uuid.uuid4()}/{action}")).status_code == 404
    assert (await viewer.get(url)).json()[0]["id"] == proposal["id"]
    assert (await outsider.get(url)).status_code == 404

    rejected = await rig.owner.post(f"{url}/{proposal['id']}/reject")
    assert rejected.status_code == 200 and rejected.json()["proposal"]["status"] == "rejected"
    assert await script_of(rig, case) == as_script(SCRIPT)
    assert (await rig.owner.get(f"{rig.base}/test-cases/{case['id']}")).json()[
        "status"
    ] == "approved"
    assert (await rig.owner.post(f"{url}/{proposal['id']}/accept")).status_code == 409
    assert await proposals(rig, case_id=case["id"], status="rejected")
    assert await proposals(rig, case_id=str(uuid.uuid4())) == []
    rows = await rig.rows(LocatorHealProposal)
    assert rows[0].decided_at is not None


# --- stability ----------------------------------------------------------------------------


async def stability(rig: Rig, **params: Any) -> list[dict[str, Any]]:
    response = await rig.owner.get(f"{rig.base}/stability", params=params)
    assert response.status_code == 200, response.text
    found: list[dict[str, Any]] = response.json()
    return found


async def test_a_case_whose_outcome_keeps_changing_is_reported_as_flaky_and_stays_visible(
    api: Api,
) -> None:
    rig = await Rig(api, execution_rate_limit=100).setup()
    steady, wobbly, broken = await rig.case("500"), await rig.case("510"), await rig.case("520")
    rig.engine.sequences = {wobbly["key"]: ["passed", "transient", "passed", "transient", "passed"]}
    rig.engine.outcomes = {broken["key"]: "failed"}
    verdicts = [(await rig.execute(steady, wobbly, broken))["verdict"] for _ in range(5)]

    assert verdicts == ["failed"] * 5  # nothing was hidden to make a run look better
    found = {item["case_key"]: item for item in await stability(rig)}
    assert found[wobbly["key"]]["is_flaky"] is True
    assert (found[wobbly["key"]]["runs"], found[wobbly["key"]]["flips"]) == (5, 4)
    assert found[wobbly["key"]]["kinds"] == ["environment"]
    assert found[wobbly["key"]]["flake_rate"] == 0.4
    # Always failing is a failure, not flakiness; always passing is neither.
    assert found[broken["key"]]["is_flaky"] is False and found[broken["key"]]["not_passed"] == 5
    assert found[steady["key"]]["is_flaky"] is False and found[steady["key"]]["passed"] == 5

    assert [item["case_key"] for item in await stability(rig, flaky_only=True)] == [wobbly["key"]]
    assert (await stability(rig))[0]["case_key"] == wobbly["key"]  # flaky first
    one = await rig.owner.get(f"{rig.base}/test-cases/{wobbly['id']}/stability")
    assert one.status_code == 200 and one.json()["is_flaky"] is True
    assert (
        await rig.owner.get(f"{rig.base}/test-cases/{uuid.uuid4()}/stability")
    ).status_code == 404
    assert (await rig.owner.get(f"{rig.base}/stability", params={"window": 1})).status_code == 422


async def test_a_pass_on_retry_stays_flaky_in_the_result_the_verdict_and_the_history(
    api: Api,
) -> None:
    rig = await Rig(api).setup()
    case = await rig.case()
    rig.engine.outcomes = {case["key"]: "flaky"}

    execution = await rig.execute(case)

    assert execution["results"][0]["status"] == "flaky" and execution["verdict"] == "flaky"
    assert (execution["totals"]["passed"], execution["totals"]["flaky"]) == (0, 1)
    found = (await rig.owner.get(f"{rig.base}/test-cases/{case['id']}/stability")).json()
    assert found["is_flaky"] is True and (found["passed"], found["passed_on_retry"]) == (0, 1)

    never_run = await rig.case("530")
    empty = (await rig.owner.get(f"{rig.base}/test-cases/{never_run['id']}/stability")).json()
    assert (empty["runs"], empty["is_flaky"], empty["last_status"]) == (0, False, None)
    outsider = await api.register("owner@other.io", organization="Other")
    assert (await outsider.get(f"{rig.base}/stability")).status_code == 404
