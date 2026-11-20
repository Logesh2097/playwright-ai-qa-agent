"""Generating, reviewing, editing and approving test cases and suites through the API."""

from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agent.llm.base import LLMTimeoutError, LLMUnavailableError
from app.agent.llm.factory import build_gateway
from tests.conftest import Account, Api, Workspace, make_settings
from tests.factories import (
    LOGIN_INSTRUCTION,
    LOYALTY_INSTRUCTION,
    case_dict,
    completion,
    generated,
    loyalty_analysis_dict,
    loyalty_cases,
)

pytestmark = pytest.mark.integration


class Review:
    """An instruction whose requirement has been approved, ready for case generation."""

    def __init__(self, api: Api, owner: Account, ws: Workspace, instruction_id: str) -> None:
        self.api, self.owner, self.ws, self.instruction_id = api, owner, ws, instruction_id
        self.instruction_url = f"{ws.project_url}/instructions/{instruction_id}"
        self.cases_url = f"{ws.project_url}/test-cases"
        self.suites_url = f"{ws.project_url}/test-suites"

    async def generate(self, cases: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if cases is not None:
            self.api.llm.script = [{"cases": cases}]
        response = await self.owner.post(f"{self.instruction_url}/generate")
        assert response.status_code == 202, response.text
        await self.api.run_jobs()
        result: dict[str, Any] = (await self.owner.get(self.instruction_url)).json()
        return result

    async def cases(self, account: Account | None = None) -> list[dict[str, Any]]:
        response = await (account or self.owner).get(
            self.cases_url, params={"instruction_id": self.instruction_id}
        )
        assert response.status_code == 200, response.text
        listed: list[dict[str, Any]] = response.json()
        return listed

    async def case(self, case_id: str) -> dict[str, Any]:
        found: dict[str, Any] = (await self.owner.get(f"{self.cases_url}/{case_id}")).json()
        return found

    async def suite(self) -> dict[str, Any]:
        suites = (await self.owner.get(self.suites_url)).json()
        found: dict[str, Any] = next(
            s for s in suites if s["instruction_id"] == self.instruction_id
        )
        return found


async def _review(api: Api, *, loyalty: bool = False, owner: Account | None = None) -> Review:
    owner = owner or await api.register("owner@acme.io")
    ws = await owner.setup_environment()
    await owner.post(
        ws.credentials_url,
        json={
            "name": "customer-account",
            "role_label": "customer",
            "kind": "username_password",
            "secret": {"username": "u", "password": "Zx9!vK-unguessable"},
            "is_test_account": True,
        },
    )
    if loyalty:
        api.llm.script = [loyalty_analysis_dict(minimum_known=True)]
    submitted = await owner.post(
        f"{ws.project_url}/instructions",
        json={
            "application_id": ws.application_id,
            "environment_id": ws.environment_id,
            "text": LOYALTY_INSTRUCTION if loyalty else LOGIN_INSTRUCTION,
        },
    )
    assert submitted.status_code == 202, submitted.text
    await api.run_jobs()
    url = f"{ws.project_url}/instructions/{submitted.json()['id']}"
    approved = await owner.post(f"{url}/approve")
    assert approved.status_code == 201, approved.text
    return Review(api, owner, ws, submitted.json()["id"])


# --- generation --------------------------------------------------------------------------


async def test_generate_cases_from_an_approved_requirement(api: Api) -> None:
    review = await _review(api)
    started = await review.owner.post(f"{review.instruction_url}/generate")
    assert started.status_code == 202, started.text
    assert started.json()["status"] == "generating"
    assert api.queue.pending[0][0] == "generate_cases", "generation runs in the worker"

    assert await api.run_jobs() == ["awaiting_approval"]
    instruction = (await review.owner.get(review.instruction_url)).json()
    assert instruction["status"] == "awaiting_approval"
    assert instruction["generation"]["state"] == "finished"
    assert instruction["generation"]["generated"] == 1
    assert instruction["generation"]["prompt"] == "test_case_generation@v1"
    assert instruction["usage"]["llm_calls"] == 2, "analysis + generation"

    listed = await review.cases()
    assert len(listed) == 1
    summary = listed[0]
    assert (summary["key"], summary["status"], summary["source"]) == (
        "TC-0001",
        "needs_review",
        "ai_generated",
    )
    assert (summary["version"], summary["version_source"], summary["ready"]) == (1, "ai", True)

    case = await review.case(summary["id"])
    content = case["content"]
    assert set(content) == {
        "title", "description", "category", "test_type", "objective_id", "preconditions", "test_data",
        "steps", "expected_result", "expected_source", "priority", "severity", "tags", "dependencies",
        "automation_feasibility", "automation_notes", "verification", "assumptions",
    }  # fmt: skip
    assert len(content["steps"]) == 5
    assert content["steps"][1]["target"] == "the Email field", (
        "elements are described, not selected"
    )
    assert content["steps"][1]["value"] == "{{cred.customer.username}}"
    assert (case["original_version"], case["approved_version"]) == (1, None)
    assert case["validation"] == {"ready": True, "issues": []}

    # The generated cases were collected into a draft suite, in order.
    suite = await review.suite()
    assert (suite["status"], suite["source"], suite["feature"]) == (
        "draft",
        "ai_generated",
        "Customer login",
    )
    assert [c["key"] for c in suite["cases"]] == ["TC-0001"]


async def test_generation_requires_an_approved_requirement(api: Api) -> None:
    owner = await api.register("owner@acme.io")
    ws = await owner.setup_environment()
    submitted = await owner.post(
        f"{ws.project_url}/instructions",
        json={
            "application_id": ws.application_id,
            "environment_id": ws.environment_id,
            "text": LOGIN_INSTRUCTION,
        },
    )
    url = f"{ws.project_url}/instructions/{submitted.json()['id']}"
    for _ in range(2):  # while analysing, and once analysed but not approved
        response = await owner.post(f"{url}/generate")
        assert (response.status_code, response.json()["code"]) == (409, "invalid_state_transition")
        await api.run_jobs()


async def test_the_model_is_given_the_plan_as_fenced_data_and_no_secrets(api: Api) -> None:
    review = await _review(api, loyalty=True)
    await review.generate(loyalty_cases())
    request = api.llm.requests[-1]

    assert request.purpose == "generation"
    assert "Zx9!vK-unguessable" not in request.system + request.user
    plan_block = request.user.split("<untrusted plan")[1]
    assert "No points are awarded." in plan_block
    assert '"objectives_to_cover"' in plan_block
    assert "customer" in request.user.split("<untrusted context")[1]


# --- boundary scenarios and missing expected results ---------------------------------------


async def test_loyalty_example_boundaries_keep_their_values_and_nothing_is_guessed(
    api: Api,
) -> None:
    review = await _review(api, loyalty=True)
    instruction = await review.generate(loyalty_cases())
    assert instruction["generation"]["generated"] == 10
    assert instruction["generation"]["needs_attention"] == 7

    listed = {c["title"]: c for c in await review.cases()}
    assert len(listed) == 10
    amounts = {}
    for title in (
        "Bill amount below the minimum",
        "Bill amount equal to the minimum",
        "Bill amount above the minimum",
    ):
        case = await review.case(listed[title]["id"])
        amounts[title] = case["content"]["test_data"][0]["value"]
        assert case["content"]["expected_source"] == "plan_objective"
        assert case["ready"] is True
    assert list(amounts.values()) == ["499", "500", "501"]

    # Scenarios beyond the plan: generated, but with no expected result and not approvable.
    for title in (
        "Empty bill amount",
        "Invalid bill amount",
        "Zero bill amount",
        "Negative bill amount",
        "Decimal bill amount",
        "Missing customer details",
        "Invalid loyalty configuration",
    ):
        case = await review.case(listed[title]["id"])
        assert case["content"]["expected_result"] is None, title
        assert case["content"]["expected_source"] == "unknown"
        assert (case["ready"], case["has_expected_result"]) == (False, False)
        issue_codes = {i["code"] for i in case["validation"]["issues"]}
        assert "missing_expected_result" in issue_codes
        assert "not_in_requirement" in issue_codes
        refused = await review.owner.post(f"{review.cases_url}/{case['id']}/approve")
        assert (refused.status_code, refused.json()["code"]) == (409, "case_not_ready")

    categories = {c["category"] for c in listed.values()}
    assert {"boundary", "form_validation", "edge_case", "negative", "business_logic"} <= categories


async def test_a_person_supplies_the_missing_expected_result(api: Api) -> None:
    review = await _review(api, loyalty=True)
    await review.generate(loyalty_cases())
    negative = next(c for c in await review.cases() if c["title"] == "Negative bill amount")
    url = f"{review.cases_url}/{negative['id']}"
    content = (await review.case(negative["id"]))["content"]

    # Claiming a source the result does not have is not possible.
    content["expected_result"] = (
        "The form shows 'Amount must be positive' and no points are awarded."
    )
    content["expected_source"] = "plan_objective"
    content["steps"][-1]["value"] = "Amount must be positive"
    edited = await review.owner.put(url, json=content)
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert body["content"]["expected_source"] == "human_specified"
    assert (body["version"], body["version_source"], body["ready"]) == (2, "human", True)

    approved = await review.owner.post(f"{url}/approve")
    assert approved.status_code == 200, approved.text
    assert (approved.json()["status"], approved.json()["approved_version"]) == ("approved", 2)


async def test_a_guessed_result_from_the_model_is_rejected_by_the_schema(api: Api) -> None:
    """Real gateway, simulated provider: 'unknown' with a result is refused, repaired once."""
    review = await _review(api)
    guessed = generated(
        objective_id=None, expected_source="unknown", expected_result="Probably an error is shown"
    )
    honest = generated(objective_id=None, expected_source="unknown", expected_result=None)
    replies = [
        httpx.Response(
            200, json=completion('{"cases": [' + __import__("json").dumps(guessed) + "]}")
        ),
        httpx.Response(
            200, json=completion('{"cases": [' + __import__("json").dumps(honest) + "]}")
        ),
    ]
    gateway = build_gateway(
        make_settings(openai_api_key="sk-test", llm_model_analysis="m", llm_retry_base_delay_s=0),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: replies.pop(0))),
    )
    await review.owner.post(f"{review.instruction_url}/generate")
    assert await api.run_jobs(gateway=gateway) == ["awaiting_approval"]
    case = await review.case((await review.cases())[0]["id"])
    assert case["content"]["expected_result"] is None
    assert not replies, "the first answer was rejected and one repair was requested"


# --- duplicates, batching, failures --------------------------------------------------------


async def test_duplicate_scenarios_are_removed(api: Api) -> None:
    review = await _review(api)
    steps = case_dict()["steps"]
    instruction = await review.generate(
        [
            generated("C1"),
            generated("C2", title="VALID LOGIN SUCCEEDS!!"),  # same title, different punctuation
            generated(
                "C3", title="Customer signs in", description="Reworded, same steps"
            ),  # same steps
            generated(
                "C4",
                title="Empty email is rejected",
                category="form_validation",
                objective_id="OBJ-3",
                steps=[steps[0], steps[3], steps[4]],
            ),
        ]
    )
    assert instruction["generation"]["generated"] == 2
    assert instruction["generation"]["duplicates_removed"] == 2
    assert [c["title"] for c in await review.cases()] == [
        "Valid login succeeds",
        "Empty email is rejected",
    ]


async def test_objectives_are_covered_in_batches_and_later_calls_know_earlier_titles(
    api: Api,
) -> None:
    review = await _review(api)  # the login plan has three objectives
    api.app.state.settings.generation_objectives_per_call = 2
    steps = case_dict()["steps"]
    api.llm.script = [
        {
            "cases": [
                generated("C1"),
                generated(
                    "C2",
                    title="Invalid password is rejected",
                    objective_id="OBJ-2",
                    category="negative",
                    steps=[*steps[:2], steps[3], steps[4]],
                ),
            ]
        },
        {
            "cases": [
                generated(
                    "C1",
                    title="Empty email is rejected",
                    objective_id="OBJ-3",
                    category="form_validation",
                    steps=[steps[0], steps[3], steps[4]],
                )
            ]
        },
    ]
    await review.owner.post(f"{review.instruction_url}/generate")
    await api.run_jobs()

    first, second = api.llm.requests[-2], api.llm.requests[-1]
    assert '"objectives_to_cover": [\n    "OBJ-1",\n    "OBJ-2"\n  ]' in first.user
    assert '"objectives_to_cover": [\n    "OBJ-3"\n  ]' in second.user
    assert "Invalid password is rejected" in second.user, "so the model does not repeat them"
    instruction = (await review.owner.get(review.instruction_url)).json()
    assert (instruction["generation"]["generated"], instruction["generation"]["llm_calls"]) == (
        3,
        2,
    )
    assert [c["key"] for c in await review.cases()] == ["TC-0001", "TC-0002", "TC-0003"]


async def test_failed_generation_leaves_the_requirement_approved_and_can_be_retried(
    api: Api,
) -> None:
    review = await _review(api)
    api.llm.script = [LLMUnavailableError()]
    await review.owner.post(f"{review.instruction_url}/generate")
    assert await api.run_jobs() == ["approved"]

    instruction = (await review.owner.get(review.instruction_url)).json()
    assert instruction["status"] == "approved"
    assert instruction["generation"]["state"] == "failed"
    assert instruction["generation"]["error"]["code"] == "llm_unavailable"
    assert await review.cases() == []

    assert (await review.generate())["status"] == "awaiting_approval"


async def test_cases_from_before_a_failure_are_kept(api: Api) -> None:
    review = await _review(api)
    api.app.state.settings.generation_objectives_per_call = 2
    api.llm.script = [{"cases": [generated("C1")]}, LLMTimeoutError()]
    await review.owner.post(f"{review.instruction_url}/generate")
    assert await api.run_jobs() == ["awaiting_approval"]

    generation = (await review.owner.get(review.instruction_url)).json()["generation"]
    assert (generation["generated"], generation["partial"]) == (1, True)
    assert generation["error"]["code"] == "llm_timeout"
    assert len(await review.cases()) == 1


async def test_generated_cases_that_break_the_rules_are_kept_but_blocked(api: Api) -> None:
    review = await _review(api)
    steps = case_dict()["steps"]
    await review.generate(
        [
            generated(
                "C1",
                title="Goes elsewhere",
                steps=[{**steps[0], "value": "https://evil.example/login"}, *steps[1:]],
            ),
            generated(
                "C2",
                title="Assumes things",
                objective_id="OBJ-2",
                steps=[steps[0], steps[3], steps[4]],
                assumptions=["The account locks after 5 attempts"],
            ),
            generated(
                "C3", title="No such objective", objective_id="OBJ-77", steps=[steps[0], steps[4]]
            ),
            generated(
                "C4",
                title="Wrong account",
                objective_id="OBJ-3",
                steps=[
                    steps[0],
                    {**steps[1], "value": "{{cred.admin.username}}"},
                    steps[3],
                    steps[4],
                ],
            ),
        ]
    )
    found = {}
    for summary in await review.cases():
        case = await review.case(summary["id"])
        found[case["title"]] = {
            i["code"] for i in case["validation"]["issues"] if i["severity"] == "blocking"
        }
    assert found == {
        "Goes elsewhere": {"out_of_scope_url"},
        "Assumes things": {"unsupported_assumptions"},
        "No such objective": {"unknown_objective"},
        "Wrong account": {"credential_missing"},
    }
    suite = await review.suite()
    refused = await review.owner.post(f"{review.suites_url}/{suite['id']}/approve")
    assert (refused.status_code, refused.json()["code"]) == (409, "suite_not_ready")
    assert {e["code"] for e in refused.json()["errors"]} == {
        "out_of_scope_url",
        "unsupported_assumptions",
        "unknown_objective",
        "credential_missing",
    }


async def test_without_an_ai_service_generation_is_refused(api: Api) -> None:
    review = await _review(api)
    api.llm.configured = False
    response = await review.owner.post(f"{review.instruction_url}/generate")
    assert (response.status_code, response.json()["code"]) == (503, "llm_not_configured")
    assert (await review.owner.get(review.instruction_url)).json()["status"] == "approved"


# --- manual editing and versions -------------------------------------------------------------


async def test_editing_creates_a_new_version_and_keeps_the_ai_original(
    api: Api, owner_engine: AsyncEngine
) -> None:
    review = await _review(api)
    await review.generate()
    case_id = (await review.cases())[0]["id"]
    url = f"{review.cases_url}/{case_id}"
    original = (await review.case(case_id))["content"]

    edited_content = {
        **original,
        "title": "Customer signs in with valid details",
        "priority": "p1",
        "tags": ["login"],
    }
    edited_content["steps"] = [
        *original["steps"][:4],
        {
            "action": "assert_url",
            "description": "The address is the account page",
            "target": None,
            "value": "/account",
            "side_effect": "none",
        },
        original["steps"][4],
    ]
    edited = await review.owner.put(url, json=edited_content)
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert (body["version"], body["version_source"], body["original_version"]) == (2, "human", 1)
    assert (body["title"], body["priority"], len(body["content"]["steps"])) == (
        "Customer signs in with valid details",
        "p1",
        6,
    )
    assert body["content"]["expected_source"] == "plan_objective", "the result was not touched"

    versions = (await review.owner.get(f"{url}/versions")).json()
    assert [
        (v["version"], v["source"], v["change_reason"], v["is_original"], v["is_current"])
        for v in versions
    ] == [
        (1, "ai", "created", True, False),
        (2, "human", "edited", False, True),
    ]
    assert versions[1]["created_by"] == review.owner.user_id and versions[0]["created_by"] is None

    # Version 1 is still exactly what the model wrote.
    first = (await review.owner.get(f"{url}/versions/1")).json()
    assert first["content"] == original
    assert (await review.owner.get(f"{url}/versions/9")).status_code == 404

    # Saving without changes does not create a version.
    assert (await review.owner.put(url, json=body["content"])).json()["version"] == 2
    async with owner_engine.connect() as connection:
        assert (
            await connection.execute(text("SELECT count(*) FROM test_case_versions"))
        ).scalar() == 2
        assert (await connection.execute(text("SELECT count(*) FROM test_steps"))).scalar() == 11


async def test_edits_go_through_the_same_schema_and_checks(api: Api) -> None:
    review = await _review(api)
    await review.generate()
    case_id = (await review.cases())[0]["id"]
    url = f"{review.cases_url}/{case_id}"
    content = (await review.case(case_id))["content"]

    for bad in (
        {**content, "expected_source": "unknown"},  # a result labelled unknown
        {**content, "status": "approved"},  # a field that does not exist
        {**content, "category": "whatever"},
        {**content, "steps": [{**content["steps"][0], "action": "run_script"}]},
        {k: v for k, v in content.items() if k != "steps"},
    ):
        assert (await review.owner.put(url, json=bad)).status_code == 422
    assert (await review.case(case_id))["version"] == 1

    # A valid edit that introduces a problem is saved, and blocked from approval.
    weaker = await review.owner.put(url, json={**content, "steps": content["steps"][:-1]})
    assert weaker.status_code == 200
    assert weaker.json()["ready"] is False
    assert {i["code"] for i in weaker.json()["validation"]["issues"]} == {"no_verification_step"}
    assert (await review.owner.post(f"{url}/approve")).status_code == 409

    # Removing the expected result is allowed and labelled honestly.
    cleared = await review.owner.put(
        url, json={**content, "expected_result": None, "expected_source": "unknown"}
    )
    assert cleared.status_code == 200
    assert "missing_expected_result" in {i["code"] for i in cleared.json()["validation"]["issues"]}


async def test_editing_an_approved_case_needs_review_again_but_keeps_the_approved_version(
    api: Api,
) -> None:
    review = await _review(api)
    await review.generate()
    case_id = (await review.cases())[0]["id"]
    url = f"{review.cases_url}/{case_id}"
    approved = (await review.owner.post(f"{url}/approve")).json()
    assert (approved["status"], approved["approved_version"]) == ("approved", 1)

    content = approved["content"]
    edited = (
        await review.owner.put(url, json={**content, "description": "Changed after approval."})
    ).json()
    assert (edited["status"], edited["version"]) == ("needs_review", 2)
    assert edited["approved_version"] == 1, "what was approved stays on record"
    versions = (await review.owner.get(f"{url}/versions")).json()
    assert [(v["version"], v["is_approved"], v["approved_by"] is not None) for v in versions] == [
        (1, True, True),
        (2, False, False),
    ]


async def test_add_a_manual_case_and_delete_an_irrelevant_one(api: Api) -> None:
    review = await _review(api)
    await review.generate()
    generated_id = (await review.cases())[0]["id"]

    manual = await review.owner.post(
        review.cases_url,
        json={
            "application_id": review.ws.application_id,
            "environment_id": review.ws.environment_id,
            "instruction_id": review.instruction_id,
            "content": case_dict(
                title="Login page is reachable from the home page",
                category="navigation",
                objective_id=None,
                steps=[case_dict()["steps"][0], case_dict()["steps"][4]],
                expected_result="The login form is displayed.",
            ),
        },
    )
    assert manual.status_code == 201, manual.text
    body = manual.json()
    assert (body["key"], body["source"], body["version_source"]) == ("TC-0002", "manual", "human")
    assert (body["original_version"], body["content"]["expected_source"]) == (
        None,
        "human_specified",
    )

    # A manual copy of an existing scenario is pointed out.
    duplicate = await review.owner.post(
        review.cases_url,
        json={
            "application_id": review.ws.application_id,
            "environment_id": review.ws.environment_id,
            "instruction_id": review.instruction_id,
            "content": case_dict(title="Sign in works"),
        },
    )
    assert "possible_duplicate" in {i["code"] for i in duplicate.json()["validation"]["issues"]}

    assert (await review.owner.delete(f"{review.cases_url}/{generated_id}")).status_code == 204
    assert (await review.owner.get(f"{review.cases_url}/{generated_id}")).status_code == 404
    assert [c["key"] for c in await review.cases()] == ["TC-0002", "TC-0003"]
    assert [c["key"] for c in (await review.suite())["cases"]] == [], (
        "removed from its suite as well"
    )

    # Keys are never reused.
    another = await review.owner.post(
        review.cases_url,
        json={
            "application_id": review.ws.application_id,
            "content": case_dict(
                title="Another", steps=[case_dict()["steps"][0], case_dict()["steps"][4]]
            ),
        },
    )
    assert another.json()["key"] == "TC-0004"
    unknown = await review.owner.post(
        review.cases_url, json={"application_id": review.ws.environment_id, "content": case_dict()}
    )
    assert (unknown.status_code, unknown.json()["code"]) == (422, "unknown_target")


async def test_regenerate_selected_cases(api: Api) -> None:
    review = await _review(api, loyalty=True)
    await review.generate(loyalty_cases()[:4])
    listed = await review.cases()
    chosen, untouched = listed[3], listed[0]
    feedback = "The steps never select a customer first."

    improved = dict(
        loyalty_cases()[3], local_id="C1", description="Rewritten with the missing step."
    )
    api.llm.script = [{"cases": [improved]}]
    started = await review.owner.post(
        f"{review.instruction_url}/regenerate",
        json={"case_ids": [chosen["id"]], "feedback": feedback},
    )
    assert started.status_code == 202, started.text
    assert started.json()["status"] == "generating"
    assert await api.run_jobs() == ["awaiting_approval"]

    request = api.llm.requests[-1]
    review_block = request.user.split("<untrusted review")[1]
    assert feedback in review_block and "Empty bill amount" in review_block
    assert feedback not in request.system

    after = await review.case(chosen["id"])
    assert (after["version"], after["version_source"], after["original_version"]) == (2, "ai", 1)
    assert after["content"]["description"] == "Rewritten with the missing step."
    versions = (await review.owner.get(f"{review.cases_url}/{chosen['id']}/versions")).json()
    assert [v["change_reason"] for v in versions] == ["created", "regenerated"]
    assert (await review.case(untouched["id"]))["version"] == 1
    assert len(await review.cases()) == 4, "no case was added or lost"
    generation = (await review.owner.get(review.instruction_url)).json()["generation"]
    assert (generation["mode"], generation["regenerated"]) == ("regenerate", 1)

    other = await _review(api, owner=review.owner)
    await other.generate()
    foreign = await review.owner.post(
        f"{review.instruction_url}/regenerate", json={"case_ids": [(await other.cases())[0]["id"]]}
    )
    assert (foreign.status_code, foreign.json()["code"]) == (422, "unknown_case")
    assert (
        await review.owner.post(f"{review.instruction_url}/regenerate", json={"case_ids": []})
    ).status_code == 422


# --- suites ----------------------------------------------------------------------------------


async def test_suite_management(api: Api) -> None:
    review = await _review(api, loyalty=True)
    await review.generate(loyalty_cases()[:4])
    ids = [c["id"] for c in await review.cases()]

    created = await review.owner.post(
        review.suites_url,
        json={
            "name": "Loyalty smoke",
            "application_id": review.ws.application_id,
            "feature": "Loyalty",
            "priority": "p0",
            "tags": ["smoke"],
            "case_ids": ids[:2],
        },
    )
    assert created.status_code == 201, created.text
    suite = created.json()
    assert (suite["status"], suite["feature"], suite["priority"], suite["source"]) == (
        "draft",
        "Loyalty",
        "p0",
        "manual",
    )
    assert [(c["position"], c["key"]) for c in suite["cases"]] == [(1, "TC-0001"), (2, "TC-0002")]
    url = f"{review.suites_url}/{suite['id']}"

    # Add, remove and reorder in one call: the list is the execution order.
    reordered = await review.owner.put(f"{url}/cases", json={"case_ids": [ids[2], ids[0], ids[3]]})
    assert [(c["position"], c["key"]) for c in reordered.json()["cases"]] == [
        (1, "TC-0003"),
        (2, "TC-0001"),
        (3, "TC-0004"),
    ]

    updated = await review.owner.patch(
        url, json={"name": "Loyalty regression", "priority": "p1", "description": "Run nightly"}
    )
    assert (updated.json()["name"], updated.json()["priority"]) == ("Loyalty regression", "p1")

    # Organised by feature; filterable.
    await review.owner.post(
        review.suites_url,
        json={
            "name": "Checkout",
            "application_id": review.ws.application_id,
            "feature": "Checkout",
        },
    )
    by_feature = (await review.owner.get(review.suites_url, params={"feature": "Loyalty"})).json()
    assert [s["name"] for s in by_feature] == ["Loyalty regression"]
    listed = (await review.owner.get(review.suites_url)).json()
    assert [s["feature"] for s in listed] == ["Checkout", "Loyalty", "Loyalty points calculation"]

    for body, code in (
        ({"case_ids": [ids[0], ids[0]]}, "duplicate_case"),
        ({"case_ids": [review.ws.application_id]}, "unknown_case"),
    ):
        refused = await review.owner.put(f"{url}/cases", json=body)
        assert (refused.status_code, refused.json()["code"]) == (422, code)
    clash = await review.owner.post(
        review.suites_url, json={"name": "Checkout", "application_id": review.ws.application_id}
    )
    assert (clash.status_code, clash.json()["code"]) == (409, "name_taken")

    assert (await review.owner.delete(url)).status_code == 204
    assert (await review.owner.get(url)).status_code == 404
    assert len(await review.cases()) == 4, "deleting a suite keeps its cases"


async def test_approving_a_suite_is_all_or_nothing_and_pins_versions(api: Api) -> None:
    review = await _review(api, loyalty=True)
    await review.generate(loyalty_cases()[:4])  # three ready, one without an expected result
    suite = await review.suite()
    url = f"{review.suites_url}/{suite['id']}"

    refused = await review.owner.post(f"{url}/approve")
    assert (refused.status_code, refused.json()["code"]) == (409, "suite_not_ready")
    assert {(e["field"], e["code"]) for e in refused.json()["errors"]} == {
        ("TC-0004", "missing_expected_result"),
        ("TC-0004", "placeholder_in_step"),
    }
    assert {c["status"] for c in await review.cases()} == {"needs_review"}, "nothing was approved"
    assert (await review.owner.get(url)).json()["status"] == "draft"

    # Drop the unfinished case from the suite and approve the rest.
    ready_ids = [c["id"] for c in suite["cases"][:3]]
    await review.owner.put(f"{url}/cases", json={"case_ids": ready_ids})
    approved = await review.owner.post(f"{url}/approve")
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert (body["status"], body["approved_by"]) == ("approved", review.owner.user_id)
    assert [(c["status"], c["approved_version"], c["outdated"]) for c in body["cases"]] == [
        ("approved", 1, False)
    ] * 3
    assert (await review.owner.get(review.instruction_url)).json()["status"] == "completed"

    # Editing a case afterwards does not change what the suite approved.
    case = await review.case(ready_ids[0])
    await review.owner.put(
        f"{review.cases_url}/{ready_ids[0]}",
        json={**case["content"], "description": "Edited later."},
    )
    after_edit = (await review.owner.get(url)).json()
    assert after_edit["status"] == "approved"
    first = after_edit["cases"][0]
    assert (first["current_version"], first["approved_version"], first["outdated"]) == (2, 1, True)
    first_version = (await review.owner.get(f"{review.cases_url}/{ready_ids[0]}/versions/1")).json()
    assert first_version["is_approved"] and first_version["content"] == case["content"]

    # Changing which cases are in it, or their order, makes it a draft again.
    reopened = await review.owner.put(f"{url}/cases", json={"case_ids": list(reversed(ready_ids))})
    assert reopened.json()["status"] == "draft"
    assert {c["approved_version"] for c in reopened.json()["cases"]} == {None}

    empty = await review.owner.post(
        review.suites_url, json={"name": "Empty", "application_id": review.ws.application_id}
    )
    nothing = await review.owner.post(f"{review.suites_url}/{empty.json()['id']}/approve")
    assert (nothing.status_code, nothing.json()["code"]) == (409, "suite_empty")


# --- permissions and isolation -----------------------------------------------------------------


async def test_who_can_edit_and_who_can_approve(api: Api) -> None:
    review = await _review(api)
    await review.generate()
    team: dict[str, Account] = {}
    for role in ("qa_engineer", "developer", "viewer"):
        team[role] = await api.invite_and_join(review.owner, f"{role}@acme.io", role)
        await review.owner.post(
            f"{review.ws.project_url}/members", json={"user_id": team[role].user_id}
        )
    case_id = (await review.cases())[0]["id"]
    url = f"{review.cases_url}/{case_id}"
    suite_url = f"{review.suites_url}/{(await review.suite())['id']}"
    content = (await review.case(case_id))["content"]

    # Everyone in the project can read cases and suites.
    for account in team.values():
        assert (await account.get(url)).status_code == 200
        assert (await account.get(suite_url)).status_code == 200

    viewer = team["viewer"]
    denied = [
        await viewer.put(url, json=content),
        await viewer.delete(url),
        await viewer.post(
            review.cases_url, json={"application_id": review.ws.application_id, "content": content}
        ),
        await viewer.post(
            review.suites_url, json={"name": "x", "application_id": review.ws.application_id}
        ),
        await viewer.put(f"{suite_url}/cases", json={"case_ids": []}),
        await viewer.post(f"{review.instruction_url}/regenerate", json={"case_ids": [case_id]}),
    ]
    assert [r.status_code for r in denied] == [403] * 6

    # Developers edit and organise; approving is for QA engineers and above.
    developer = team["developer"]
    assert (await developer.put(url, json={**content, "tags": ["reviewed"]})).status_code == 200
    for target in (f"{url}/approve", f"{suite_url}/approve"):
        refused = await developer.post(target)
        assert (refused.status_code, refused.json()["code"]) == (403, "insufficient_role")
    assert (await team["qa_engineer"].post(f"{url}/approve")).status_code == 200
    assert (await team["qa_engineer"].post(f"{suite_url}/approve")).status_code == 200


async def test_cases_and_suites_are_isolated_between_tenants_and_projects(api: Api) -> None:
    review = await _review(api)
    await review.generate()
    case_id = (await review.cases())[0]["id"]
    suite_id = (await review.suite())["id"]
    content = (await review.case(case_id))["content"]

    attacker = await api.register("owner@evil.io", organization="Evil")
    own = await attacker.setup_environment()
    targets = [
        review.cases_url,
        f"{review.cases_url}/{case_id}",
        f"{review.cases_url}/{case_id}/versions",
        review.suites_url,
        f"{review.suites_url}/{suite_id}",
    ]
    for target in targets:
        assert (await attacker.get(target)).status_code == 404
        assert (
            await attacker.get(target.replace(review.owner.org_id, attacker.org_id))
        ).status_code == 404
    # The victim's IDs inside the attacker's own project.
    assert (await attacker.get(f"{own.project_url}/test-cases/{case_id}")).status_code == 404
    assert (
        await attacker.put(f"{own.project_url}/test-cases/{case_id}", json=content)
    ).status_code == 404
    assert (
        await attacker.post(f"{own.project_url}/test-suites/{suite_id}/approve")
    ).status_code == 404
    stolen = await attacker.post(
        f"{own.project_url}/test-suites",
        json={"name": "Mine", "application_id": own.application_id, "case_ids": [case_id]},
    )
    assert (stolen.status_code, stolen.json()["code"]) == (422, "unknown_case")
    assert (await attacker.get(f"{own.project_url}/test-cases")).json() == []

    # A colleague outside the project sees none of it either.
    outsider = await api.invite_and_join(review.owner, "dev@acme.io", "developer")
    assert (await outsider.get(f"{review.cases_url}/{case_id}")).status_code == 404


async def test_archived_project_is_read_only_for_cases_and_suites(api: Api) -> None:
    review = await _review(api)
    await review.generate()
    case_id = (await review.cases())[0]["id"]
    content = (await review.case(case_id))["content"]
    await review.owner.post(f"{review.ws.project_url}/archive")

    assert (await review.owner.get(f"{review.cases_url}/{case_id}")).status_code == 200
    for response in (
        await review.owner.put(f"{review.cases_url}/{case_id}", json={**content, "tags": ["x"]}),
        await review.owner.post(f"{review.cases_url}/{case_id}/approve"),
        await review.owner.post(
            review.suites_url, json={"name": "x", "application_id": review.ws.application_id}
        ),
    ):
        assert (response.status_code, response.json()["code"]) == (409, "project_archived")
