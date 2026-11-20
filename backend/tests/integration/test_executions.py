"""Executions through the API with the database and Redis real and the engine faked.

What is under test here is everything around the engine: which cases may run,
what the engine is told, how its events become stored steps, results, evidence
and a verdict, and who may see them.
"""

import asyncio
import base64
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app.db.context import set_current_org
from app.engine.client import EngineError
from app.models import AuditLog, TestEvidence, TestExecutionStep
from tests.conftest import Account, Api, Workspace
from tests.engine_fakes import PNG, FakeEngine
from tests.mcp_fakes import FakeInfrastructure

pytestmark = pytest.mark.integration

PASSWORD_VALUE = "Zx9!vK-unguessable-77"
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
    {"action": "expect", "url_path": "/account"},
]
SCRIPT: dict[str, Any] = {
    "steps": [
        {"action": "navigate", "url": "/loyalty"},
        {
            "action": "fill",
            "locator": {"by": "label", "value": "Bill amount"},
            "value": "{{data.bill_amount}}",
        },
        {
            "action": "click",
            "locator": {"by": "role", "role": "button", "name": "Calculate points"},
        },
        {
            "action": "read_text",
            "locator": {"by": "test_id", "value": "points"},
            "save_as": "points",
            "as_number": True,
        },
        {
            "action": "assert_expression",
            "expression": "points == floor(number(data.bill_amount) / 10)",
        },
    ]
}
LOOK_ONLY: dict[str, Any] = {
    "steps": [
        {"action": "navigate", "url": "/loyalty"},
        {"action": "assert_title", "title": {"equals": "Loyalty points"}},
    ]
}


def content(amount: str) -> dict[str, Any]:
    return {
        "title": f"Loyalty points for a bill of {amount}",
        "description": "The points shown for a bill follow the loyalty rule.",
        "category": "business_logic",
        "test_type": "ui",
        "objective_id": None,
        "preconditions": [],
        "test_data": [{"name": "bill_amount", "value": amount, "description": None}],
        "steps": [
            {
                "action": "navigate",
                "description": "Open the loyalty page",
                "target": None,
                "value": "/loyalty",
                "side_effect": "none",
            },
            {
                "action": "fill",
                "description": "Enter the bill amount",
                "target": "the Bill amount field",
                "value": "{{data.bill_amount}}",
                "side_effect": "none",
            },
            {
                "action": "click",
                "description": "Calculate the points",
                "target": "the Calculate points button",
                "value": None,
                "side_effect": "none",
            },
            {
                "action": "assert_text",
                "description": "Check the points that are shown",
                "target": "the points earned",
                "value": str(int(amount) // 10),
                "side_effect": "none",
            },
        ],
        "expected_result": f"{int(amount) // 10} points are shown",
        "expected_source": "human_specified",
        "priority": "p1",
        "severity": "major",
        "tags": [],
        "dependencies": [],
        "automation_feasibility": "automatable",
        "automation_notes": None,
        "verification": {
            "method": "ui_assertion",
            "description": "The points shown equal one per 10 spent, rounded down",
        },
        "assumptions": [],
    }


class Rig:
    def __init__(self, api: Api, **settings: Any) -> None:
        self.api = api
        self.engine = FakeEngine()
        self.infra = FakeInfrastructure()
        api.engine = self.engine
        api.egress = self.infra.egress_client()
        api.app.state.settings = api.app.state.settings.model_copy(
            update={"engine_url": "http://engine:8902", **settings}
        )
        self.owner: Account
        self.ws: Workspace

    async def setup(self, **environment: Any) -> "Rig":
        self.owner = await self.api.register("owner@acme.io")
        environment.setdefault("is_read_only", False)
        environment.setdefault("requires_approval", False)
        self.ws = await self.owner.setup_environment(**environment)
        return self

    @property
    def base(self) -> str:
        return self.ws.project_url

    async def case(
        self,
        amount: str = "500",
        script: dict[str, Any] | None = SCRIPT,
        *,
        approve: bool = True,
        ready: bool = True,
    ) -> dict[str, Any]:
        created = await self.owner.post(
            f"{self.base}/test-cases",
            json={
                "application_id": self.ws.application_id,
                "environment_id": self.ws.environment_id,
                "content": content(amount),
            },
        )
        assert created.status_code == 201, created.text
        case: dict[str, Any] = created.json()
        if script is not None:
            saved = await self.owner.put(
                f"{self.base}/test-cases/{case['id']}/automation", json=script
            )
            assert saved.status_code == 200, saved.text
            assert saved.json()["validation"]["ready"] is ready, saved.json()["validation"]
        if approve:
            approved = await self.owner.post(f"{self.base}/test-cases/{case['id']}/approve")
            assert approved.status_code == 200, approved.text
        return case

    async def start(self, account: Account | None = None, **body: Any) -> Any:
        body.setdefault("environment_id", self.ws.environment_id)
        return await (account or self.owner).post(f"{self.base}/executions", json=body)

    async def execute(self, *cases: dict[str, Any], **body: Any) -> dict[str, Any]:
        started = await self.start(case_ids=[c["id"] for c in cases], **body)
        assert started.status_code == 202, started.text
        await self.api.run_jobs()
        return await self.get(started.json()["id"])

    async def get(self, execution_id: str) -> dict[str, Any]:
        response = await self.owner.get(f"{self.base}/executions/{execution_id}")
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body

    async def rows(self, model: Any) -> list[Any]:
        async with self.api.app.state.session_factory() as session, session.begin():
            await set_current_org(session, uuid.UUID(self.owner.org_id))
            return list((await session.execute(select(model))).scalars())


# --- scripts on cases ---------------------------------------------------------------------


async def test_a_script_is_a_new_version_that_needs_approval_and_survives_edits(api: Api) -> None:
    rig = await Rig(api).setup()
    case = await rig.case(script=None)
    url = f"{rig.base}/test-cases/{case['id']}"
    assert (await rig.owner.get(f"{url}/automation")).json()["script"] is None

    saved = (await rig.owner.put(f"{url}/automation", json=SCRIPT)).json()
    assert (saved["version"], saved["status"]) == (2, "needs_review")
    assert saved["script"]["steps"][0] == {
        "action": "navigate",
        "url": "/loyalty",
        "description": None,
        "timeout_ms": None,
    }
    assert saved["validation"]["ready"] is True
    # Saving the same script again changes nothing.
    assert (await rig.owner.put(f"{url}/automation", json=SCRIPT)).json()["version"] == 2

    edited = content("500")
    edited["description"] = "Reworded."
    assert (await rig.owner.put(url, json=edited)).status_code == 200
    kept = (await rig.owner.get(f"{url}/automation")).json()
    assert kept["version"] == 3 and len(kept["script"]["steps"]) == 5

    removed = (await rig.owner.delete(f"{url}/automation")).json()
    assert removed["script"] is None and removed["version"] == 4
    actions = [r.action for r in await rig.rows(AuditLog)]
    assert actions.count("test_case.automation") == 2


async def test_invalid_scripts_are_rejected_and_unusable_ones_block_approval(api: Api) -> None:
    rig = await Rig(api).setup()
    case = await rig.case(script=None, approve=False)
    url = f"{rig.base}/test-cases/{case['id']}"
    invalid: list[dict[str, Any]] = [
        {"steps": []},
        {"steps": [{"action": "evaluate", "script": "alert(1)"}]},
        {"steps": [{"action": "click", "locator": {"by": "css", "value": "//button"}}]},
        {"steps": [{"action": "assert_expression", "expression": "__import__('os')"}]},
    ]
    for broken in invalid:
        assert (await rig.owner.put(f"{url}/automation", json=broken)).status_code == 422

    unusable = {
        "steps": [
            {"action": "navigate", "url": "https://evil.example/"},
            {
                "action": "fill",
                "locator": {"by": "label", "value": "Amount"},
                "value": "{{data.nope}}",
            },
        ]
    }
    saved = (await rig.owner.put(f"{url}/automation", json=unusable)).json()
    found = {
        issue["code"] for issue in saved["validation"]["issues"] if issue["severity"] == "blocking"
    }
    assert found == {
        "automation_url_not_allowed",
        "automation_placeholder_unknown",
        "automation_no_assertion",
    }
    refused = await rig.owner.post(f"{url}/approve")
    assert (refused.status_code, refused.json()["code"]) == (409, "case_not_ready")

    assert (await rig.owner.put(f"{url}/automation", json=SCRIPT)).json()["validation"]["ready"]
    assert (await rig.owner.post(f"{url}/approve")).status_code == 200


# --- the normal run -----------------------------------------------------------------------


async def test_an_approved_case_runs_and_everything_about_it_is_stored(api: Api) -> None:
    rig = await Rig(api).setup()
    case = await rig.case("500")

    started = await rig.start(case_ids=[case["id"]])
    assert started.status_code == 202
    queued = started.json()
    assert (queued["status"], queued["verdict"]) == ("queued", None)
    assert queued["results"][0]["status"] == "pending"
    assert await api.run_jobs() == ["completed:passed"]

    execution = await rig.get(queued["id"])
    assert (execution["status"], execution["verdict"]) == ("completed", "passed")
    assert execution["totals"] == {
        "total": 1,
        "passed": 1,
        "failed": 0,
        "error": 0,
        "flaky": 0,
        "skipped": 0,
        "blocked": 0,
        "needs_review": 0,
    }
    assert (
        execution["started_at"]
        and execution["finished_at"]
        and execution["triggered_by"] == rig.owner.user_id
    )
    result = execution["results"][0]
    assert (result["case_key"], result["status"], result["attempt_count"]) == (
        case["key"],
        "passed",
        1,
    )
    assert (result["browser"], result["viewport"], result["version"]) == ("chromium", "1280x720", 2)
    assert result["started_at"] and result["finished_at"] and result["duration_ms"] == 2000

    steps = (
        await rig.owner.get(f"{rig.base}/executions/{queued['id']}/results/{result['id']}/steps")
    ).json()
    assert [(s["position"], s["phase"], s["action"], s["status"]) for s in steps] == [
        (1, "main", "navigate", "passed"),
        (2, "main", "fill", "passed"),
        (3, "main", "click", "passed"),
        (4, "main", "read_text", "passed"),
        (5, "main", "assert_expression", "passed"),
    ]
    assert steps[4]["expected"] and steps[4]["actual"] and steps[4]["error_type"] is None
    assert all(s["started_at"] and s["finished_at"] and s["duration_ms"] == 120 for s in steps)
    assert steps[1]["locator"] == {"by": "role", "matched": 1}
    assert rig.engine.deleted == [queued["id"]]  # the engine's copy of the run is removed


async def test_the_engine_is_told_exactly_what_the_environment_allows(api: Api) -> None:
    rig = await Rig(api).setup(variables=[{"name": "REGION", "value": "in"}])
    case = await rig.case("505")
    config = {
        "browser": "firefox",
        "viewport": {"width": 390, "height": 844},
        "retry": {"max_attempts": 2},
    }

    execution = await rig.execute(case, config=config)

    plan = rig.engine.plans[0]
    assert plan.run_id == execution["id"]
    assert (plan.base_url, plan.allowed_origins) == (
        "https://staging.acme.io",
        ["https://staging.acme.io"],
    )
    assert plan.read_only is False and "click" in plan.allowed_actions
    assert plan.env == {"REGION": "in"}
    assert plan.cases[0].data == {"bill_amount": "505"}
    assert plan.cases[0].key == case["key"] and len(plan.cases[0].script.steps) == 5
    assert (plan.config.browser, plan.config.viewport.label, plan.config.retry.max_attempts) == (
        "firefox",
        "390x844",
        2,
    )
    assert plan.credentials == {} and plan.files == []  # nothing the script does not use
    assert (
        execution["results"][0]["browser"] == "firefox"
        and execution["results"][0]["viewport"] == "390x844"
    )
    # The browser's way out is opened for this run only, and closed afterwards.
    assert plan.proxy is not None and plan.proxy.username == execution["id"]
    assert rig.infra.order() == ["egress PUT", "egress DELETE"]
    assert rig.infra.requests[0][2]["allowed_origins"] == ["https://staging.acme.io"]
    assert rig.infra.requests[0][2]["secret"] == plan.proxy.password


async def test_invalid_run_configuration_is_rejected(api: Api) -> None:
    rig = await Rig(api).setup()
    case = await rig.case()
    for config in (
        {"browser": "netscape"},
        {"timeouts": {"test_ms": 1}},
        {"retry": {"max_attempts": 9}},
        {"headless": False},
    ):
        assert (await rig.start(case_ids=[case["id"]], config=config)).status_code == 422
    assert (await rig.start()).status_code == 422  # neither a suite nor cases
    assert (await rig.start(case_ids=[case["id"]], suite_id=str(uuid.uuid4()))).status_code == 422


# --- verdicts -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcomes", "verdict", "job"),
    [
        (["passed", "passed"], "passed", "completed:passed"),
        (["passed", "failed"], "failed", "completed:failed"),
        (["passed", "error"], "error", "completed:error"),
        (["failed", "error"], "failed", "completed:failed"),
        (["passed", "flaky"], "flaky", "completed:flaky"),
    ],
)
async def test_the_verdict_follows_from_the_results(
    api: Api, outcomes: list[str], verdict: str, job: str
) -> None:
    rig = await Rig(api).setup()
    cases = [await rig.case(str(500 + n * 10)) for n in range(len(outcomes))]
    rig.engine.outcomes = {
        case["key"]: outcome for case, outcome in zip(cases, outcomes, strict=True)
    }

    started = await rig.start(case_ids=[c["id"] for c in cases])
    assert await api.run_jobs() == [job]
    execution = await rig.get(started.json()["id"])

    assert execution["verdict"] == verdict
    assert [r["status"] for r in execution["results"]] == outcomes
    for result, outcome in zip(execution["results"], outcomes, strict=True):
        assert (result["failure_class"] is not None) is (outcome in ("failed", "error"))
        assert result["attempt_count"] == (2 if outcome == "flaky" else 1)


async def test_a_failed_case_keeps_its_evidence_and_both_attempts_of_a_flaky_one_are_stored(
    api: Api,
) -> None:
    rig = await Rig(api).setup()
    failing, flaky = await rig.case("500"), await rig.case("510")
    rig.engine.outcomes = {failing["key"]: "failed", flaky["key"]: "flaky"}

    execution = await rig.execute(failing, flaky)

    url = f"{rig.base}/executions/{execution['id']}"
    failed, wobbly = execution["results"]
    assert (
        failed["failure_summary"] == f"{failing['key']} did not pass"
        and failed["failure_class"] == "assertion"
    )
    steps = (await rig.owner.get(f"{url}/results/{failed['id']}/steps")).json()
    last = steps[-1]
    assert (last["status"], last["error_type"], last["error_message"]) == (
        "failed",
        "assertion",
        "expected 50, found 51",
    )
    assert last["expected"] == {"equals": "50"} and last["actual"] == {"text": "51"}

    evidence = (await rig.owner.get(f"{url}/evidence")).json()
    assert len(evidence) == 2  # the failure, and the first attempt of the flaky case
    shot = next(e for e in evidence if e["result_id"] == failed["id"])
    assert (shot["kind"], shot["content_type"], shot["size_bytes"]) == (
        "screenshot",
        "image/png",
        len(PNG),
    )
    assert shot["step_id"] == last["id"] and last["evidence_ids"] == [shot["id"]]
    assert shot["expires_at"] and len(shot["sha256"]) == 64
    downloaded = await rig.owner.get(f"{url}/evidence/{shot['id']}/content")
    assert downloaded.status_code == 200 and downloaded.content == PNG
    assert downloaded.headers["content-type"] == "image/png"
    assert "attachment" in downloaded.headers["content-disposition"]
    assert downloaded.headers["x-content-type-options"] == "nosniff"

    attempts = (await rig.owner.get(f"{url}/results/{wobbly['id']}/steps")).json()
    assert sorted({s["attempt"] for s in attempts}) == [1, 2]
    assert [s["status"] for s in attempts if s["attempt"] == 1][-1] == "failed"
    assert all(s["status"] == "passed" for s in attempts if s["attempt"] == 2)


async def test_a_case_that_did_not_run_keeps_the_execution_from_passing(api: Api) -> None:
    rig = await Rig(api).setup()
    automated, described_only = await rig.case("500"), await rig.case("510", script=None)

    execution = await rig.execute(automated, described_only)

    assert (execution["status"], execution["verdict"]) == ("completed", "inconclusive")
    ran, skipped = execution["results"]
    assert ran["status"] == "passed"
    assert (skipped["status"], skipped["failure_summary"]) == (
        "skipped",
        "The case has no automation script.",
    )
    assert len(rig.engine.plans[0].cases) == 1


async def test_nothing_runnable_means_no_run_and_no_pass(api: Api) -> None:
    rig = await Rig(api).setup()
    described_only = await rig.case(script=None)

    started = await rig.start(case_ids=[described_only["id"]])

    assert started.status_code == 202
    assert (started.json()["status"], started.json()["verdict"]) == ("completed", "inconclusive")
    assert api.queue.pending == [] and rig.engine.plans == []


# --- only approved work runs --------------------------------------------------------------


async def test_unapproved_cases_and_suites_cannot_be_run(api: Api) -> None:
    rig = await Rig(api).setup()
    draft = await rig.case(approve=False)
    refused = await rig.start(case_ids=[draft["id"]])
    assert (refused.status_code, refused.json()["code"]) == (409, "case_not_approved")
    assert (await rig.start(case_ids=[str(uuid.uuid4())])).status_code == 404

    suite = await rig.owner.post(
        f"{rig.base}/test-suites",
        json={
            "name": "Loyalty",
            "application_id": rig.ws.application_id,
            "case_ids": [draft["id"]],
        },
    )
    assert suite.status_code == 201, suite.text
    not_yet = await rig.start(suite_id=suite.json()["id"])
    assert (not_yet.status_code, not_yet.json()["code"]) == (409, "suite_not_approved")
    assert (await rig.start(suite_id=str(uuid.uuid4()))).status_code == 404
    assert api.queue.pending == []


async def test_a_suite_runs_the_versions_that_were_approved_with_it(api: Api) -> None:
    rig = await Rig(api).setup()
    first, second = await rig.case("500"), await rig.case("510")
    suite = (
        await rig.owner.post(
            f"{rig.base}/test-suites",
            json={
                "name": "Loyalty",
                "application_id": rig.ws.application_id,
                "case_ids": [first["id"], second["id"]],
            },
        )
    ).json()
    assert (
        await rig.owner.post(f"{rig.base}/test-suites/{suite['id']}/approve")
    ).status_code == 200
    # A later change to a case does not alter what the approved suite runs.
    changed = {**SCRIPT, "steps": [*SCRIPT["steps"], {"action": "screenshot"}]}
    await rig.owner.put(f"{rig.base}/test-cases/{first['id']}/automation", json=changed)

    started = await rig.start(suite_id=suite["id"])
    assert started.status_code == 202, started.text
    await api.run_jobs()

    execution = await rig.get(started.json()["id"])
    assert execution["suite_id"] == suite["id"] and execution["verdict"] == "passed"
    assert [len(c.script.steps) for c in rig.engine.plans[0].cases] == [5, 5]
    assert [r["version"] for r in execution["results"]] == [2, 2]


async def test_a_case_whose_script_no_longer_fits_the_environment_is_blocked_not_run(
    api: Api,
) -> None:
    rig = await Rig(api).setup()
    case = await rig.case("500")
    # The environment is made read-only after the case was approved.
    assert (
        await rig.owner.patch(rig.ws.environment_url, json={"is_read_only": True})
    ).status_code == 200

    started = await rig.start(case_ids=[case["id"]])

    body = started.json()
    assert (body["status"], body["verdict"]) == ("completed", "inconclusive")
    assert body["results"][0]["status"] == "blocked"
    assert "environment" in body["results"][0]["failure_summary"]
    assert body["results"][0]["failure_class"] == "test_data"
    assert rig.engine.plans == []


# --- test data, credentials and files -----------------------------------------------------


async def test_data_sets_supply_values_and_environment_specific_ones_win(api: Api) -> None:
    rig = await Rig(api).setup()
    sets = f"{rig.base}/test-data-sets"
    general = await rig.owner.post(
        sets,
        json={
            "name": "checkout",
            "values": {"coupon": "WELCOME", "city": "Pune"},
            "is_synthetic": True,
        },
    )
    assert general.status_code == 201, general.text
    specific = await rig.owner.post(
        sets,
        json={
            "name": "checkout",
            "environment_id": rig.ws.environment_id,
            "values": {"coupon": "STAGING10"},
            "is_synthetic": True,
        },
    )
    assert specific.status_code == 201, specific.text
    script = {
        "data_sets": ["checkout"],
        "steps": [
            {"action": "navigate", "url": "/loyalty?coupon={{data.coupon}}&city={{data.city}}"},
            {"action": "assert_title", "title": {"contains": "{{data.bill_amount}}"}},
        ],
    }
    case = await rig.case("500", script)

    await rig.execute(case)

    # The environment's value replaces the general one; the case's own data is added.
    assert rig.engine.plans[0].cases[0].data == {
        "coupon": "STAGING10",
        "city": "Pune",
        "bill_amount": "500",
    }
    listed = (await rig.owner.get(sets)).json()
    assert [(d["name"], d["environment_id"]) for d in listed] == [
        ("checkout", None),
        ("checkout", rig.ws.environment_id),
    ]

    updated = await rig.owner.put(
        f"{sets}/{general.json()['id']}",
        json={"name": "checkout", "values": {"coupon": "HELLO"}, "is_synthetic": True},
    )
    assert updated.json()["values"] == {"coupon": "HELLO"}
    assert (await rig.owner.delete(f"{sets}/{specific.json()['id']}")).status_code == 204


async def test_test_data_must_be_declared_synthetic_and_real_card_numbers_are_refused(
    api: Api,
) -> None:
    rig = await Rig(api).setup()
    sets = f"{rig.base}/test-data-sets"
    base = {"name": "payment", "values": {"card": "4242 4242 4242 4242"}, "is_synthetic": True}
    for body, code in (
        ({**base, "is_synthetic": False}, "real_data_not_allowed"),
        ({**base, "values": {"card": "4485 2757 4230 8327"}}, "real_data_not_allowed"),
        ({**base, "name": "not a name"}, "validation_error"),
        ({**base, "values": {"bad key": "x"}}, "validation_error"),
        ({**base, "environment_id": str(uuid.uuid4())}, "not_found"),
    ):
        response = await rig.owner.post(sets, json=body)
        assert response.json()["code"] == code, body
    assert (
        await rig.owner.post(sets, json=base)
    ).status_code == 201  # a provider's test number is fine
    duplicate = await rig.owner.post(sets, json=base)
    assert (duplicate.status_code, duplicate.json()["code"]) == (409, "name_taken")


async def test_credentials_reach_the_engine_only_when_a_script_uses_them_and_never_the_database(
    api: Api,
) -> None:
    rig = await Rig(api).setup()
    created = await rig.owner.post(
        rig.ws.credentials_url,
        json={
            "name": "customer", "role_label": "customer", "kind": "username_password",
            "secret": {"username": "demo", "password": PASSWORD_VALUE},
            "is_test_account": True, "login_recipe": RECIPE,
        },
    )  # fmt: skip
    assert created.status_code == 201, created.text
    signed_in = await rig.case("500", {"login_as": "customer", **SCRIPT})
    anonymous = await rig.case("510")

    first = await rig.execute(anonymous)
    assert rig.engine.plans[0].credentials == {}
    second = await rig.execute(signed_in)

    grant = rig.engine.plans[1].credentials["customer"]
    assert (
        grant.fields == {"username": "demo", "password": PASSWORD_VALUE}
        and len(grant.login_recipe) == 5
    )
    steps = (
        await rig.owner.get(
            f"{rig.base}/executions/{second['id']}/results/{second['results'][0]['id']}/steps"
        )
    ).json()
    assert (steps[0]["phase"], steps[0]["action"], steps[0]["position"]) == ("setup", "login", 1)
    # Each use is on record, and the secret is in none of what was stored or returned.
    used = [r for r in await rig.rows(AuditLog) if r.action == "credential.use"]
    assert len(used) == 1 and used[0].changes["purpose"] == "execution"
    assert used[0].changes["reference_id"] == second["id"]
    stored = str(
        [(r.actual, r.expected, r.error_message) for r in await rig.rows(TestExecutionStep)]
    )
    assert PASSWORD_VALUE not in stored + str(first) + str(second) + str(steps)
    assert PASSWORD_VALUE not in str([r.changes for r in await rig.rows(AuditLog)])


async def test_only_approved_test_files_can_be_uploaded_and_only_qa_can_add_them(api: Api) -> None:
    rig = await Rig(api).setup()
    developer = await api.invite_and_join(rig.owner, "dev@acme.io", "developer")
    await rig.owner.post(
        f"{rig.base}/members", json={"user_id": developer.user_id, "role": "member"}
    )
    files = f"{rig.base}/test-files"
    body = {
        "name": "receipt.pdf", "content_type": "application/pdf",
        "content_base64": base64.b64encode(b"%PDF-1.4 test receipt").decode(), "is_synthetic": True,
    }  # fmt: skip

    assert (await developer.post(files, json=body)).status_code == 403
    for bad, code in (
        ({**body, "is_synthetic": False}, "real_data_not_allowed"),
        ({**body, "content_type": "application/x-msdownload"}, "file_type_not_allowed"),
        ({**body, "content_base64": "not base64!"}, "invalid_file"),
        ({**body, "content_base64": ""}, "file_size_not_allowed"),
        ({**body, "name": "../../etc/passwd"}, "validation_error"),
    ):
        assert (await rig.owner.post(files, json=bad)).json()["code"] == code, bad
    created = await rig.owner.post(files, json=body)
    assert created.status_code == 201
    assert set(created.json()) == {
        "id",
        "name",
        "content_type",
        "size_bytes",
        "sha256",
        "created_at",
    }
    assert (await rig.owner.post(files, json=body)).status_code == 409

    upload = {
        "action": "upload_file",
        "locator": {"by": "label", "value": "Receipt"},
        "file": "receipt.pdf",
    }
    check = {"action": "assert_visible", "locator": {"by": "role", "role": "status"}}
    case = await rig.case(
        "500", {"steps": [{"action": "navigate", "url": "/upload"}, upload, check]}
    )
    await rig.execute(case)
    sent = rig.engine.plans[0].files
    assert [(f.name, f.content_type) for f in sent] == [("receipt.pdf", "application/pdf")]
    assert base64.b64decode(sent[0].content_base64) == b"%PDF-1.4 test receipt"

    # A script naming a file that is not there cannot be approved.
    unknown = await rig.case(
        "510",
        {
            "steps": [
                {"action": "navigate", "url": "/upload"},
                {**upload, "file": "payroll.xlsx"},
                check,
            ]
        },
        approve=False,
        ready=False,
    )
    refused = await rig.owner.post(f"{rig.base}/test-cases/{unknown['id']}/approve")
    assert refused.status_code == 409 and "payroll.xlsx" in refused.text
    assert (await rig.owner.delete(f"{files}/{created.json()['id']}")).status_code == 204
    assert (await developer.get(files)).json() == []


# --- who may run and see what -------------------------------------------------------------


async def test_viewers_can_read_results_but_not_start_or_cancel_and_other_organizations_see_nothing(
    api: Api,
) -> None:
    rig = await Rig(api).setup()
    case = await rig.case()
    rig.engine.outcomes = {case["key"]: "failed"}
    execution = await rig.execute(case)
    viewer = await api.invite_and_join(rig.owner, "viewer@acme.io", "viewer")
    await rig.owner.post(f"{rig.base}/members", json={"user_id": viewer.user_id, "role": "member"})
    outsider = await api.register("owner@other.io", organization="Other")
    url = f"{rig.base}/executions/{execution['id']}"
    evidence = (await rig.owner.get(f"{url}/evidence")).json()[0]

    assert (await rig.start(viewer, case_ids=[case["id"]])).status_code == 403
    assert (await viewer.post(f"{url}/cancel")).status_code == 403
    assert (await viewer.get(url)).json()["verdict"] == "failed"
    assert (await viewer.get(f"{rig.base}/executions")).json()[0]["id"] == execution["id"]
    assert (await viewer.get(f"{url}/evidence/{evidence['id']}/content")).status_code == 200

    for path in (
        url,
        f"{url}/evidence",
        f"{url}/evidence/{evidence['id']}/content",
        f"{rig.base}/executions",
    ):
        assert (await outsider.get(path)).status_code == 404, path
    assert (
        await outsider.post(
            f"{rig.base}/executions",
            json={"environment_id": rig.ws.environment_id, "case_ids": [case["id"]]},
        )
    ).status_code == 404
    # Evidence of one execution is not reachable through another.
    other = await rig.execute(case)
    assert (
        await rig.owner.get(
            f"{rig.base}/executions/{other['id']}/evidence/{evidence['id']}/content"
        )
    ).status_code in (200, 404)
    assert (await rig.owner.get(f"{rig.base}/executions/{uuid.uuid4()}")).status_code == 404


async def test_changing_data_on_an_approval_environment_needs_an_admin(api: Api) -> None:
    rig = await Rig(api).setup(requires_approval=True)
    engineer = await api.invite_and_join(rig.owner, "qa@acme.io", "qa_engineer")
    await rig.owner.post(
        f"{rig.base}/members", json={"user_id": engineer.user_id, "role": "member"}
    )
    changing, looking = await rig.case("500"), await rig.case("510", LOOK_ONLY)

    refused = await rig.start(engineer, case_ids=[changing["id"]])
    assert (refused.status_code, refused.json()["code"]) == (403, "approval_required")
    assert (await rig.start(engineer, case_ids=[looking["id"]])).status_code == 202  # only looks
    await api.run_jobs()
    assert set(rig.engine.plans[0].allowed_actions) == {"navigate", "read", "api_read"}

    approved = await rig.execute(changing)
    assert approved["verdict"] == "passed" and approved["config"]["state_changes_approved"] is True
    assert "click" in rig.engine.plans[1].allowed_actions
    started = [r for r in await rig.rows(AuditLog) if r.action == "execution.start"]
    assert [r.changes["state_changes_approved"] for r in started] == [False, True]


# --- failures around the engine -----------------------------------------------------------


async def test_installations_without_an_engine_answer_503(api: Api) -> None:
    rig = await Rig(api, engine_url=None).setup()
    case = await rig.case()
    response = await rig.start(case_ids=[case["id"]])
    assert (response.status_code, response.json()["code"]) == (503, "engine_not_configured")


@pytest.mark.parametrize(
    ("break_it", "status", "code"),
    [
        (
            lambda rig: setattr(
                rig.engine, "unavailable", EngineError("engine_unavailable", "down")
            ),
            "failed",
            "engine_unavailable",
        ),
        (
            lambda rig: setattr(rig.engine, "unavailable", EngineError("engine_busy", "full")),
            "failed",
            "engine_busy",
        ),
        (lambda rig: setattr(rig.api, "engine", None), "failed", "engine_not_configured"),
        (lambda rig: setattr(rig.infra, "egress_status", 500), "failed", "runner_unavailable"),
    ],
)
async def test_an_execution_that_cannot_reach_the_engine_fails_without_a_pass(
    api: Api, break_it: Any, status: str, code: str
) -> None:
    rig = await Rig(api).setup()
    case = await rig.case()
    break_it(rig)

    execution = await rig.execute(case)

    assert (execution["status"], execution["error"]["code"]) == (status, code)
    assert execution["verdict"] == "inconclusive"
    assert execution["results"][0]["status"] == "skipped"
    assert not rig.infra.registered


async def test_an_engine_that_stops_mid_run_leaves_the_running_case_as_an_error(api: Api) -> None:
    rig = await Rig(api).setup()
    cases = [await rig.case(str(500 + n * 10)) for n in range(3)]
    rig.engine.abort_after_cases = 1

    execution = await rig.execute(*cases)

    assert (execution["status"], execution["error"]["code"]) == ("failed", "internal")
    assert [r["status"] for r in execution["results"]] == ["passed", "error", "skipped"]
    assert execution["results"][1]["failure_class"] == "infrastructure"
    assert execution["verdict"] == "error"


async def test_evidence_that_cannot_be_collected_is_noted_and_does_not_change_the_result(
    api: Api,
) -> None:
    rig = await Rig(api).setup()
    case = await rig.case()
    rig.engine.outcomes = {case["key"]: "failed"}
    rig.engine.missing_artifacts = True

    execution = await rig.execute(case)

    assert execution["results"][0]["status"] == "failed"
    assert execution["results"][0]["details"]["evidence_notes"] == [
        "screenshot_not_stored:artifact_missing"
    ]
    assert await rig.rows(TestEvidence) == []


# --- cancelling, limits, time -------------------------------------------------------------


async def test_cancelling_a_queued_execution_means_it_never_runs(api: Api) -> None:
    rig = await Rig(api).setup()
    case = await rig.case()
    started = (await rig.start(case_ids=[case["id"]])).json()

    cancelled = await rig.owner.post(f"{rig.base}/executions/{started['id']}/cancel")

    assert (cancelled.status_code, cancelled.json()["status"]) == (200, "cancelled")
    assert await api.run_jobs() == ["discarded"]
    execution = await rig.get(started["id"])
    assert (execution["status"], execution["verdict"]) == ("cancelled", "inconclusive")
    assert execution["results"][0]["status"] == "skipped" and rig.engine.plans == []
    again = await rig.owner.post(f"{rig.base}/executions/{started['id']}/cancel")
    assert (again.status_code, again.json()["code"]) == (409, "execution_ended")


async def test_cancelling_a_running_execution_stops_the_engine(api: Api) -> None:
    rig = await Rig(api).setup()
    cases = [await rig.case("500"), await rig.case("510")]
    rig.engine.hang_in_case = 1
    started = (await rig.start(case_ids=[c["id"] for c in cases])).json()
    job = asyncio.create_task(api.run_jobs())
    await asyncio.wait_for(rig.engine.started.wait(), timeout=10)

    cancelling = await rig.owner.post(f"{rig.base}/executions/{started['id']}/cancel")
    assert cancelling.json()["status"] == "cancelling"
    assert await asyncio.wait_for(job, timeout=15) == ["cancelled:error"]

    assert rig.engine.stream_closed  # closing the stream is what stops the run
    execution = await rig.get(started["id"])
    assert execution["status"] == "cancelled"
    assert [r["status"] for r in execution["results"]] == ["passed", "error"]
    assert "cancelled" in execution["results"][1]["failure_summary"]
    assert not rig.infra.registered and rig.engine.deleted == [started["id"]]


async def test_an_execution_that_takes_too_long_is_stopped(api: Api) -> None:
    rig = await Rig(api, execution_timeout_s=30).setup()
    case = await rig.case()
    rig.engine.hang_in_case = 0
    # The setting has a floor of 30 seconds; the runner is given a shorter one directly.
    api.app.state.settings = api.app.state.settings.model_copy(update={"execution_timeout_s": 1})

    execution = await rig.execute(case)

    assert (execution["status"], execution["error"]["code"]) == ("timed_out", "execution_timeout")
    assert execution["results"][0]["status"] == "error" and execution["verdict"] == "error"
    assert rig.engine.stream_closed


async def test_executions_are_limited_per_organization(api: Api) -> None:
    rig = await Rig(api, execution_max_active=2, execution_limit=3).setup()
    case = await rig.case()
    body: dict[str, Any] = {"case_ids": [case["id"]]}
    assert (await rig.start(**body)).status_code == 202
    assert (await rig.start(**body)).status_code == 202
    busy = await rig.start(**body)
    assert (busy.status_code, busy.json()["code"]) == (429, "too_many_executions")
    await api.run_jobs()  # both finish
    assert (await rig.start(**body)).status_code == 202
    await api.run_jobs()
    limited = await rig.start(**body)
    assert limited.status_code == 429 and "Retry-After" in limited.headers
