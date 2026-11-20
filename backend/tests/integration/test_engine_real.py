"""Executions end to end with nothing faked: API, worker code, the engine service,
real browsers, the egress proxy and the demo application.

    make be-test-browser

No model is configured or called anywhere in these tests.
"""

import asyncio
import os
import uuid
from typing import Any

import pytest

from app.browser.services import EgressClient
from app.core.security.url_safety import DestinationPolicy, parse_origin
from app.engine.client import EngineClient
from app.engine.protocol import RunPlan
from tests.conftest import Account, Api

pytestmark = [pytest.mark.browser, pytest.mark.integration]

DEMO = "http://demo-app:8080"
USERNAME, PASSWORD = "demo", "demo-password"
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


def loyalty(path: str = "/loyalty") -> dict[str, Any]:
    return {
        "steps": [
            {"action": "navigate", "url": path},
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
                "description": "One point for every 10 spent, rounded down",
                "expression": "points == floor(number(data.bill_amount) / 10)",
            },
        ]
    }


def content(title: str, amount: str = "500") -> dict[str, Any]:
    return {
        "title": title,
        "description": "Runs against the demo application.",
        "category": "business_logic",
        "test_type": "ui",
        "objective_id": None,
        "preconditions": [],
        "test_data": [{"name": "bill_amount", "value": amount, "description": None}],
        "steps": [
            {
                "action": "navigate",
                "description": "Open the page under test",
                "target": None,
                "value": "/loyalty",
                "side_effect": "none",
            },
            {
                "action": "assert_text",
                "description": "Check what the page shows",
                "target": "the result on the page",
                "value": "as expected",
                "side_effect": "none",
            },
        ],
        "expected_result": "The page behaves as the script checks",
        "expected_source": "human_specified",
        "priority": "p1",
        "severity": "major",
        "tags": [],
        "dependencies": [],
        "automation_feasibility": "automatable",
        "automation_notes": None,
        "verification": {
            "method": "ui_assertion",
            "description": "Every assertion of the script holds",
        },
        "assumptions": [],
    }


def engine_client() -> EngineClient:
    return EngineClient(os.environ["ENGINE_URL"], os.environ["ENGINE_TOKEN"])


class Stack:
    def __init__(self, api: Api) -> None:
        self.api = api
        api.engine = engine_client()
        api.egress = EgressClient(
            os.environ["EGRESS_ADMIN_URL"],
            os.environ["EGRESS_ADMIN_TOKEN"],
            os.environ["EGRESS_PROXY_URL"],
        )
        api.app.state.settings = api.app.state.settings.model_copy(
            update={"engine_url": os.environ["ENGINE_URL"]}
        )
        api.network.hosts["demo-app"] = ["10.0.0.9"]
        self.owner: Account
        self.base = ""
        self.environment_id = ""
        self.application_id = ""
        self.environment_url = ""

    async def setup(self) -> "Stack":
        self.owner = await self.api.register("owner@acme.io")
        project = await self.owner.create_project()
        application = await self.owner.create_application(project["id"], base_url=DEMO)
        environment = await self.owner.create_environment(
            project["id"], application["id"], name="demo", kind="development", base_url=DEMO,
            allow_private_network=True, is_read_only=False, requires_approval=False,
        )  # fmt: skip
        self.base = f"{self.owner.projects_url}/{project['id']}"
        self.application_id, self.environment_id = application["id"], environment["id"]
        self.environment_url = (
            f"{self.base}/applications/{application['id']}/environments/{environment['id']}"
        )
        return self

    async def case(self, title: str, script: dict[str, Any], amount: str = "500") -> dict[str, Any]:
        created = await self.owner.post(
            f"{self.base}/test-cases",
            json={
                "application_id": self.application_id,
                "environment_id": self.environment_id,
                "content": content(title, amount),
            },
        )
        assert created.status_code == 201, created.text
        url = f"{self.base}/test-cases/{created.json()['id']}"
        saved = await self.owner.put(f"{url}/automation", json=script)
        assert saved.status_code == 200 and saved.json()["validation"]["ready"], saved.text
        assert (await self.owner.post(f"{url}/approve")).status_code == 200
        case: dict[str, Any] = created.json()
        return case

    async def execute(self, *cases: dict[str, Any], **config: Any) -> dict[str, Any]:
        started = await self.owner.post(
            f"{self.base}/executions",
            json={
                "environment_id": self.environment_id,
                "case_ids": [c["id"] for c in cases],
                "config": config,
            },
        )
        assert started.status_code == 202, started.text
        await self.api.run_jobs()
        return (await self.owner.get(f"{self.base}/executions/{started.json()['id']}")).json()  # type: ignore[no-any-return]

    async def steps(self, execution: dict[str, Any], index: int = 0) -> list[dict[str, Any]]:
        result = execution["results"][index]
        url = f"{self.base}/executions/{execution['id']}/results/{result['id']}/steps"
        return (await self.owner.get(url)).json()  # type: ignore[no-any-return]


async def test_good_behaviour_passes_and_a_seeded_defect_fails_with_evidence(api: Api) -> None:
    stack = await Stack(api).setup()
    correct = await stack.case("Points for a bill of 506", loyalty(), "506")
    defective = await stack.case(
        "Points for a bill of 506 on the defective build",
        loyalty("/loyalty?variant=rounding"),
        "506",
    )

    execution = await stack.execute(correct, defective)

    assert (execution["status"], execution["verdict"]) == ("completed", "failed")
    passed, failed = execution["results"]
    assert (passed["status"], failed["status"], failed["failure_class"]) == (
        "passed",
        "failed",
        "assertion",
    )
    assert "floor" in failed["failure_summary"]

    steps = await stack.steps(execution, 1)
    rule = steps[-1]
    assert (rule["action"], rule["status"], rule["description"]) == (
        "assert_expression", "failed", "One point for every 10 spent, rounded down",
    )  # fmt: skip
    assert rule["actual"] == {
        "holds": False,
        "values": {"data.bill_amount": "506", "var.points": "51"},
    }
    assert steps[1]["locator"] == {"by": "label", "value": "Bill amount", "matched": 1}
    assert all(s["started_at"] <= s["finished_at"] for s in steps)

    evidence = (await stack.owner.get(f"{stack.base}/executions/{execution['id']}/evidence")).json()
    assert [(e["kind"], e["result_id"], e["step_id"]) for e in evidence] == [
        ("screenshot", failed["id"], rule["id"])
    ]
    image = await stack.owner.get(
        f"{stack.base}/executions/{execution['id']}/evidence/{evidence[0]['id']}/content"
    )
    assert image.status_code == 200 and image.content.startswith(b"\x89PNG")
    assert len(image.content) == evidence[0]["size_bytes"]
    # The engine keeps nothing once the worker has collected the evidence.
    assert (await engine_client().health())["runs"] == 0


async def test_signed_in_case_with_video_and_trace_and_no_password_anywhere(api: Api) -> None:
    stack = await Stack(api).setup()
    created = await stack.owner.post(
        f"{stack.environment_url}/credentials",
        json={
            "name": "customer", "role_label": "customer", "kind": "username_password",
            "secret": {"username": USERNAME, "password": PASSWORD},
            "is_test_account": True, "login_recipe": RECIPE,
        },
    )  # fmt: skip
    assert created.status_code == 201, created.text
    script = {
        "login_as": "customer",
        "steps": [
            {"action": "navigate", "url": "/account"},
            {
                "action": "assert_text",
                "locator": {"by": "text", "value": "Points balance"},
                "text": {"contains": "120"},
            },
            {"action": "api_request", "url": "/api/whoami", "save_as": "me"},
            {
                "action": "assert_api_json",
                "response": "me",
                "pointer": "/signed_in",
                "equals": True,
            },
        ],
    }
    case = await stack.case("Account page shows the points balance", script)

    execution = await stack.execute(
        case, evidence={"screenshots": "always", "video": "always", "trace": "always"}
    )

    assert execution["verdict"] == "passed", execution["results"]
    steps = await stack.steps(execution)
    assert (steps[0]["phase"], steps[0]["action"], steps[0]["status"]) == (
        "setup",
        "login",
        "passed",
    )
    evidence = (await stack.owner.get(f"{stack.base}/executions/{execution['id']}/evidence")).json()
    assert {e["kind"] for e in evidence} == {"screenshot", "video", "trace"}
    assert PASSWORD not in str(execution) + str(steps) + str(evidence)
    trace = next(e for e in evidence if e["kind"] == "trace")
    archive = await stack.owner.get(
        f"{stack.base}/executions/{execution['id']}/evidence/{trace['id']}/content"
    )
    # Signing in happened before recording started, so the trace cannot hold the password.
    assert archive.content[:2] == b"PK" and PASSWORD.encode() not in archive.content


async def test_the_browser_cannot_leave_the_allowed_origins(api: Api) -> None:
    stack = await Stack(api).setup()
    heading = {"action": "assert_visible", "locator": {"by": "role", "role": "heading"}}
    escape = await stack.case(
        "Redirect to another site",
        {"steps": [{"action": "navigate", "url": "/redirect?to=http://example.com/"}, heading]},
    )
    tracker = await stack.case(
        "Page with outside content",
        {"steps": [{"action": "navigate", "url": "/external"}, heading]},
    )

    execution = await stack.execute(escape, tracker)

    assert [r["status"] for r in execution["results"]] == ["error", "passed"], execution["results"]
    assert execution["verdict"] == "error"
    step = (await stack.steps(execution, 0))[0]
    assert step["error_type"] in ("destination_not_allowed", "navigation"), step
    # The second page loaded; its request to another site did not leave the browser.
    blocked = execution["results"][1]["details"]["blocked_requests"]
    assert [b["reason"] for b in blocked] == ["origin_not_allowlisted"]


async def test_platform_services_are_unreachable_from_a_test_even_if_allowlisted() -> None:
    """Straight to the engine, with a plan that allowlists the platform's own services."""
    run_id = uuid.uuid4()
    origins = [DEMO, "http://engine:8902", "http://runner:8900", "http://egress-proxy:8901"]
    egress = EgressClient(
        os.environ["EGRESS_ADMIN_URL"],
        os.environ["EGRESS_ADMIN_TOKEN"],
        os.environ["EGRESS_PROXY_URL"],
    )
    secret = "proxy-secret-for-this-test-0123456789"
    await egress.register(
        run_id,
        secret,
        DestinationPolicy(
            allowed_origins=frozenset(parse_origin(o) for o in origins), allow_private_network=True
        ),
        ttl_s=120,
    )
    heading = {"action": "assert_visible", "locator": {"by": "role", "role": "heading"}}
    plan = RunPlan.model_validate(
        {
            "run_id": str(run_id),
            "config": {"timeouts": {"navigation_ms": 8000}},
            "base_url": DEMO,
            "allowed_origins": origins,
            "allowed_actions": ["navigate", "read", "api_read"],
            "proxy": {"server": os.environ["EGRESS_PROXY_URL"], "username": str(run_id), "password": secret},
            "cases": [
                {
                    "result_id": str(uuid.uuid4()), "key": f"TC-{n}", "title": url,
                    "script": {"steps": [{"action": "navigate", "url": url}, heading]},
                }
                for n, url in enumerate(
                    [f"{DEMO}/", "http://engine:8902/livez", "http://runner:8900/livez", "http://egress-proxy:8901/livez"]
                )
            ]
            + [
                {
                    "result_id": str(uuid.uuid4()), "key": "TC-api", "title": "by API request",
                    "script": {"steps": [{"action": "api_request", "url": "http://engine:8902/livez", "save_as": "r"},
                                         {"action": "assert_api_status", "response": "r", "status": 200}]},
                }
            ],
        }
    )  # fmt: skip
    engine = engine_client()
    try:
        events = [event async for event in engine.run(plan)]
    finally:
        await engine.delete_run(str(run_id))
        refused = await egress.unregister(run_id)

    finished = [e for e in events if e.event == "case_finished"]
    assert [e.status for e in finished] == ["passed", "error", "error", "error", "error"], [
        e.failure_summary for e in finished
    ]
    assert secret not in str([e.model_dump() for e in events])
    hosts = {(entry["host"], entry["reason"]) for entry in refused}
    assert {
        ("engine", "platform_address"),
        ("runner", "platform_address"),
        ("egress-proxy", "platform_address"),
    } <= hosts


async def test_cancelling_stops_the_browser_run_in_the_engine(api: Api) -> None:
    stack = await Stack(api).setup()
    slow = await stack.case(
        "Slow page",
        {
            "steps": [
                {"action": "navigate", "url": "/slow?seconds=40", "timeout_ms": 60000},
                {"action": "assert_title", "title": {"equals": "Slow page"}},
            ]
        },
    )
    started = await stack.owner.post(
        f"{stack.base}/executions",
        json={"environment_id": stack.environment_id, "case_ids": [slow["id"]]},
    )
    job = asyncio.create_task(api.run_jobs())
    url = f"{stack.base}/executions/{started.json()['id']}"
    for _ in range(100):
        if (await stack.owner.get(url)).json()["results"][0]["status"] == "running":
            break
        await asyncio.sleep(0.2)

    assert (await stack.owner.post(f"{url}/cancel")).json()["status"] == "cancelling"
    assert await asyncio.wait_for(job, timeout=30) == ["cancelled:error"]

    execution = (await stack.owner.get(url)).json()
    assert execution["status"] == "cancelled" and execution["results"][0]["status"] == "error"
    for _ in range(50):
        if (await engine_client().health())["runs"] == 0:
            break
        await asyncio.sleep(0.2)
    assert (await engine_client().health())["runs"] == 0


async def test_the_same_suite_gives_the_same_results_run_after_run(api: Api) -> None:
    stack = await Stack(api).setup()
    cases = [
        await stack.case(f"Points for a bill of {amount}", loyalty(), amount)
        for amount in ("499", "500", "501")
    ]
    cases.append(
        await stack.case("Defective build at 506", loyalty("/loyalty?variant=rounding"), "506")
    )
    suite = await stack.owner.post(
        f"{stack.base}/test-suites",
        json={
            "name": "Loyalty boundaries",
            "application_id": stack.application_id,
            "case_ids": [c["id"] for c in cases],
        },
    )
    assert (
        await stack.owner.post(f"{stack.base}/test-suites/{suite.json()['id']}/approve")
    ).status_code == 200

    outcomes = []
    for _ in range(3):
        started = await stack.owner.post(
            f"{stack.base}/executions",
            json={"environment_id": stack.environment_id, "suite_id": suite.json()["id"]},
        )
        await api.run_jobs()
        execution = (
            await stack.owner.get(f"{stack.base}/executions/{started.json()['id']}")
        ).json()
        outcomes.append((execution["verdict"], [r["status"] for r in execution["results"]]))

    assert outcomes == [("failed", ["passed", "passed", "passed", "failed"])] * 3
    # The model stand-in was never asked for anything.
    assert api.llm.requests == []


# --- classification and locator recovery, for real (Phase 9) ------------------------------

OLD_PROFILE_SCRIPT: dict[str, Any] = {
    "steps": [
        {"action": "navigate", "url": "/healing"},
        {
            "action": "fill",
            "locator": {"by": "label", "value": "Email address", "exact": True},
            "value": "ada@example.com",
        },
        {
            "action": "click",
            "locator": {"by": "role", "role": "button", "name": "Save", "exact": True},
        },
        {
            "action": "assert_text",
            "locator": {"by": "role", "role": "status"},
            "text": {"equals": "Saved ada@example.com"},
        },
    ]
}
QUICK = {"timeouts": {"action_ms": 1500}}


async def test_a_renamed_page_is_recovered_reviewed_accepted_and_then_passes_as_written(
    api: Api,
) -> None:
    """The whole path of a replacement locator, with a real browser and nothing faked."""
    stack = await Stack(api).setup()
    case = await stack.case("Saving the profile e-mail", OLD_PROFILE_SCRIPT)
    proposals_url = f"{stack.base}/heal-proposals"

    # 1. Without recovery the script cannot find its field: the test's problem, not the application's.
    plain = await stack.execute(case, **QUICK)
    assert (plain["verdict"], plain["results"][0]["status"]) == ("error", "error")
    assert plain["results"][0]["failure_category"] == "test_automation_failure"
    assert (await stack.owner.get(proposals_url)).json() == []

    # 2. With recovery every step passes, and that is still not reported as a pass.
    recovered = await stack.execute(case, healing={"mode": "recover"}, **QUICK)
    result = recovered["results"][0]
    assert (recovered["verdict"], result["status"], result["healed"]) == (
        "needs_review",
        "needs_review",
        True,
    )
    assert recovered["totals"]["passed"] == 0
    steps = await stack.steps(recovered)
    assert [s["status"] for s in steps] == ["passed"] * 4
    assert [s["healed_from"] is not None for s in steps] == [False, True, True, False]
    assert steps[3]["expected"] == {"equals": "Saved ada@example.com"}  # checked as written
    found = (await stack.owner.get(proposals_url)).json()
    assert sorted(p["new_locator"]["name"] for p in found) == ["E-mail address", "Save changes"]
    assert all(p["status"] == "verified" and p["verified_by_rerun"] for p in found)

    # 3. A person accepts both; the case then needs approval before the new locators are used.
    for proposal in found:
        accepted = await stack.owner.post(f"{proposals_url}/{proposal['id']}/accept")
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["case_status"] == "needs_review"
    case_url = f"{stack.base}/test-cases/{case['id']}"
    assert (await stack.owner.post(f"{case_url}/approve")).status_code == 200

    # 4. Now it passes as written, with recovery off.
    final = await stack.execute(case, **QUICK)
    assert (final["verdict"], final["results"][0]["status"]) == ("passed", "passed")
    assert final["results"][0]["healed"] is False
    assert all(s["healed_from"] is None for s in await stack.steps(final))


async def test_real_failures_are_put_in_the_right_category(api: Api) -> None:
    stack = await Stack(api).setup()
    wrong_value = await stack.case(
        "Points with the rounding defect", loyalty("/loyalty?variant=rounding"), "506"
    )
    missing = await stack.case(
        "A button that is not there",
        {
            "steps": [
                {"action": "navigate", "url": "/loyalty"},
                {"action": "click", "locator": {"by": "role", "role": "button", "name": "Redeem"}},
                {"action": "assert_title", "title": {"equals": "Loyalty points"}},
            ]
        },
    )
    server_error = await stack.case(
        "An API that answers 500",
        {"steps": [{"action": "api_request", "method": "GET", "url": "/api/broken", "save_as": "r"},
                   {"action": "assert_api_status", "response": "r", "status": 200}]},
    )  # fmt: skip

    execution = await stack.execute(
        wrong_value, missing, server_error, healing={"mode": "recover"}, **QUICK
    )

    found = {r["title"]: (r["status"], r["failure_category"]) for r in execution["results"]}
    assert found == {
        "Points with the rounding defect": ("failed", "test_assertion_failure"),
        # Recovery looked and found nothing like a "Redeem" button: nothing was clicked.
        "A button that is not there": ("error", "test_automation_failure"),
        "An API that answers 500": ("failed", "application_failure"),
    }
    assert (await stack.owner.get(f"{stack.base}/heal-proposals")).json() == []
    report = (
        await stack.owner.get(
            f"{stack.base}/executions/{execution['id']}/results/{execution['results'][0]['id']}/failure"
        )
    ).json()
    assert report["ai_suggestion"] is None  # no model is configured, and none is needed
    assert report["record"]["failed_step"]["action"] == "assert_expression"
    assert report["record"]["screenshot_evidence_ids"]


# --- reports and evidence, for real (Phase 10) --------------------------------------------


async def test_a_real_run_gives_a_report_with_real_evidence_in_every_format(api: Api) -> None:
    stack = await Stack(api).setup()
    good = await stack.case("Points at 500", loyalty(), "500")
    bad = await stack.case(
        "Points with the rounding defect", loyalty("/loyalty?variant=rounding"), "506"
    )

    execution = await stack.execute(
        good, bad, evidence={"video": "on_failure", "trace": "on_failure", "logs": "always"}
    )

    url = f"{stack.base}/executions/{execution['id']}"
    report = (await stack.owner.get(f"{url}/report")).json()
    assert (
        report["totals"]["passed"],
        report["totals"]["failed"],
        report["totals"]["pass_rate"],
    ) == (1, 1, 0.5)
    assert report["environment"]["base_url"] == DEMO and report["execution"]["duration_ms"] > 0
    failed = report["results"][1]
    kinds = sorted(item["kind"] for item in failed["evidence"])
    # Real files passed the content checks: a PNG, a WebM video and a zipped trace.
    assert {"screenshot", "trace", "video"} <= set(kinds), kinds
    step = next(s for s in failed["steps"] if s["status"] == "failed")
    assert (
        step["expected"] is not None
        and step["actual"] is not None
        and step["duration_ms"] is not None
    )
    assert failed["evidence"][0]["expires_at"]

    shot = next(item for item in failed["evidence"] if item["kind"] == "screenshot")
    image = await stack.owner.get(f"{url}/evidence/{shot['id']}/content")
    assert (
        image.content.startswith(b"\x89PNG\r\n\x1a\n") and len(image.content) == shot["size_bytes"]
    )
    address = (await stack.owner.post(f"{url}/evidence/{shot['id']}/download-url")).json()["url"]
    assert (await api.client().get(address)).content == image.content

    pdf = await stack.owner.get(f"{url}/report/export", params={"format": "pdf"})
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-")
    # The screenshot is in the file: it is far larger than the text alone.
    text_only = await stack.owner.get(
        f"{stack.base}/executions/{(await stack.execute(good))['id']}/report/export",
        params={"format": "pdf"},
    )
    assert len(pdf.content) > len(text_only.content) + 5_000
    csv_file = await stack.owner.get(f"{url}/report/export", params={"format": "csv"})
    assert csv_file.status_code == 200 and b"test_assertion_failure" in csv_file.content


# --- the demo scenarios through the whole platform (Phase 14) -----------------------------


async def test_every_demo_scenario_through_the_platform_ends_as_it_should(api: Api) -> None:
    from tests.demo_scenarios import scenarios

    stack = await Stack(api).setup()
    chosen = scenarios()
    cases = {s.key: await stack.case(s.title, s.script) for s in chosen}
    wide = [s for s in chosen if not s.config]
    narrow = [s for s in chosen if s.config]

    first = await stack.execute(*(cases[s.key] for s in wide), timeouts={"action_ms": 2500})
    second = await stack.execute(
        *(cases[s.key] for s in narrow), timeouts={"action_ms": 2500}, **narrow[0].config
    )

    assert first["status"] == "completed", (first["status"], first["error"])
    found = {
        r["title"]: (r["status"], r["failure_category"])
        for r in [*first["results"], *second["results"]]
    }
    assert found == {s.title: (s.expected, s.category) for s in chosen}
    # Ten scenarios pass, one fails, one errors, one meets a server error: never reported as a pass.
    assert (first["verdict"], first["totals"]["passed"], first["totals"]["failed"], first["totals"]["error"]) == (
        "failed", 8, 2, 1,
    )  # fmt: skip
    assert (second["verdict"], second["totals"]["passed"]) == ("passed", 1)
    assert second["config"]["viewport"] == {"width": 400, "height": 800}
    report = (await stack.owner.get(f"{stack.base}/executions/{first['id']}/report")).json()
    assert report["failure_categories"] == {
        "test_assertion_failure": 1, "application_failure": 1, "test_automation_failure": 1,
    }  # fmt: skip
    assert len(report["failures"]) == 3 and all(
        f["screenshot_ids"] or f["status"] == "failed" for f in report["failures"]
    )
