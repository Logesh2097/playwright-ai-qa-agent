"""The whole workflow against real browsers: MCP discovery through the runner and the
egress proxy, execution by the engine service, the demo application as the target.

    make be-test-browser

Only the model is a stand-in, because its answers have to be the same on every
run. Everything it is given and everything done with its answers is real.
"""

import os
import uuid
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.agent.schemas.script import GeneratedScripts
from app.browser.factory import build_session_manager
from app.browser.services import EgressClient
from app.db.context import set_current_org
from app.engine.client import EngineClient
from app.models import WorkflowCheckpoint
from tests.conftest import Api
from tests.factories import LOGIN_INSTRUCTION
from tests.workflow_fakes import WorkflowModel, by_label, by_role, step

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


ALERT = {"by": "role", "role": "alert", "name": None, "value": None, "exact": False}


def demo_script(key: str, *, wrong_message: bool = False) -> dict[str, Any]:
    """What a model that had read the inventory would write for the two login cases."""
    form = [
        step("navigate", url="/login"),
        step("fill", locator=by_role("textbox", "Username"), value="{{cred.customer.username}}"),
    ]
    if key == "TC-0001":
        steps = [
            *form,
            step(
                "fill", locator=by_role("textbox", "Password"), value="{{cred.customer.password}}"
            ),
            step("click", locator=by_role("button", "Sign in")),
            step("assert_url", comparison="equals", expected="/account"),
            step("assert_visible", locator=by_role("heading", "Your account")),
        ]
    else:
        message = "Your account is locked" if wrong_message else "Invalid username or password"
        steps = [
            *form,
            step("fill", locator=by_label("Password"), value="not-the-right-one"),
            step("click", locator=by_role("button", "Sign in")),
            step("assert_text", locator=ALERT, comparison="equals", expected=message),
            step("assert_url", comparison="equals", expected="/login"),
        ]
    return {"case_key": key, "login_as": None, "steps": steps, "cleanup": [], "unresolved": []}


class Stack:
    def __init__(self, api: Api) -> None:
        self.api = api
        self.model = WorkflowModel(default_script=demo_script)
        api.llm = self.model  # type: ignore[assignment]
        api.app.state.llm_gateway = self.model
        api.engine = EngineClient(os.environ["ENGINE_URL"], os.environ["ENGINE_TOKEN"])
        api.egress = EgressClient(
            os.environ["EGRESS_ADMIN_URL"],
            os.environ["EGRESS_ADMIN_TOKEN"],
            os.environ["EGRESS_PROXY_URL"],
        )
        settings = api.app.state.settings.model_copy(
            update={
                "engine_url": os.environ["ENGINE_URL"],
                "runner_url": os.environ["RUNNER_URL"],
                "runner_token": SecretStr(os.environ["RUNNER_TOKEN"]),
                "egress_admin_url": os.environ["EGRESS_ADMIN_URL"],
                "egress_admin_token": SecretStr(os.environ["EGRESS_ADMIN_TOKEN"]),
                "egress_proxy_url": os.environ["EGRESS_PROXY_URL"],
            }
        )
        api.app.state.settings = settings
        api.browser_manager = build_session_manager(settings)
        api.network.hosts["demo-app"] = ["10.0.0.9"]

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
        created = await self.owner.post(
            f"{self.base}/applications/{application['id']}/environments/{environment['id']}/credentials",
            json={
                "name": "customer", "role_label": "customer", "kind": "username_password",
                "secret": {"username": USERNAME, "password": PASSWORD},
                "is_test_account": True, "login_recipe": RECIPE,
            },
        )  # fmt: skip
        assert created.status_code == 201, created.text
        return self

    async def run_to_end(self) -> dict[str, Any]:
        started = await self.owner.post(
            f"{self.base}/workflows",
            json={
                "application_id": self.application_id,
                "environment_id": self.environment_id,
                "text": LOGIN_INSTRUCTION,
            },
        )
        assert started.status_code == 202, started.text
        await self.api.run_jobs()
        url = f"{self.base}/workflows/{started.json()['id']}"
        waiting = (await self.owner.get(url)).json()
        assert waiting["status"] == "waiting_for_approval", (
            waiting["status"],
            waiting["error"],
            waiting["waiting_for"],
        )
        assert all(c["ready"] for c in waiting["waiting_for"]["cases"]), waiting["waiting_for"][
            "cases"
        ]
        assert (
            await self.owner.post(f"{url}/approval", json={"decision": "approve"})
        ).status_code == 200
        await self.api.run_jobs()
        return (await self.owner.get(url)).json()  # type: ignore[no-any-return]


async def test_test_the_customer_login_functionality_from_instruction_to_report(api: Api) -> None:
    stack = await Stack(api).setup()

    done = await stack.run_to_end()

    assert (done["status"], done["verdict"], done["error"]) == ("completed", "passed", None)
    report = (await stack.owner.get(f"{stack.base}/workflows/{done['id']}/report")).json()
    assert report["totals"]["passed"] == 2 and [r["status"] for r in report["results"]] == [
        "passed",
        "passed",
    ]
    # The application was really looked at, read-only, and what was seen reached the script prompt.
    assert (
        report["application"]["inspected_with"] == "mcp_read_only"
        and report["application"]["pages_seen"] >= 2
    )
    prompt = stack.model.of(GeneratedScripts)[0].user
    for seen in ('"Username"', '"Password"', '"Sign in"', '"Demo shop"'):
        assert seen in prompt, seen
    assert done["usage"]["mcp_calls"] >= 4 and done["usage"]["llm_calls"] == 3
    assert report["usage"]["browser_actions"] == 12
    # The password is nowhere in what the model was sent, the state, or the report.
    assert PASSWORD not in "".join(r.user for r in stack.model.requests)
    async with api.app.state.session_factory() as session, session.begin():
        await set_current_org(session, uuid.UUID(stack.owner.org_id))
        checkpoints = (await session.execute(select(WorkflowCheckpoint))).scalars().all()
    assert checkpoints and PASSWORD.encode() not in b"".join(c.checkpoint for c in checkpoints)
    assert PASSWORD not in str(report) + str(done)


async def test_a_script_that_expects_the_wrong_thing_fails_for_real_and_is_not_retried(
    api: Api,
) -> None:
    stack = await Stack(api).setup()
    stack.model.default_script = lambda key: demo_script(key, wrong_message=True)

    done = await stack.run_to_end()

    assert (done["status"], done["verdict"], done["retry_count"]) == ("completed", "failed", 0)
    report = (await stack.owner.get(f"{stack.base}/workflows/{done['id']}/report")).json()
    failed = next(r for r in report["results"] if r["status"] == "failed")
    assert failed["case_key"] == "TC-0002" and failed["failure_class"] == "assertion"
    assert failed["step_that_did_not_pass"]["actual"] == {"text": "Invalid username or password"}
    assert failed["evidence_ids"] and failed["explanation"]["advisory"] is True
    assert len(done["execution_ids"]) == 1


async def test_the_complete_platform_workflow_in_fifteen_steps(api: Api) -> None:
    """Phase 14's end-to-end list, in order, with nothing faked but the model's wording.

    1 register and log in · 2 project · 3 application · 4 environment (all in `setup`)
    5 instruction · 6 requirement analysis · 7 case generation · 8 review and approval
    9 execution · 10 Playwright MCP · 11 deterministic assertions · 12 evidence
    13 failure analysis · 14 report · 15 execution history
    """
    stack = await Stack(api).setup()  # steps 1-4
    assert (await stack.owner.get("/api/v1/auth/me")).json()["user"]["email"] == "owner@acme.io"
    stack.model.default_script = lambda key: demo_script(key, wrong_message=True)

    done = await stack.run_to_end()  # steps 5-9, pausing for the approval of step 8

    nodes = [h["node"] for h in done["history"] if h["event"] == "node_end"]
    assert nodes[:7] == [
        "receive_instruction", "analyse_requirement", "validate_application", "generate_test_plan",
        "generate_test_cases", "validate_test_cases", "approval_checkpoint",
    ]  # fmt: skip
    assert (done["status"], done["verdict"]) == ("completed", "failed")
    instruction = (
        await stack.owner.get(f"{stack.base}/instructions/{done['instruction_id']}")
    ).json()
    assert instruction["analysis"]["test_objectives"]  # 6: the requirement was analysed
    cases = (
        await stack.owner.get(
            f"{stack.base}/test-cases", params={"instruction_id": done["instruction_id"]}
        )
    ).json()
    assert len(cases) == 2 and {c["status"] for c in cases} == {"approved"}  # 7, 8

    report = (await stack.owner.get(f"{stack.base}/workflows/{done['id']}/report")).json()
    assert report["application"]["inspected_with"] == "mcp_read_only"  # 10
    assert done["usage"]["mcp_calls"] >= 4

    execution_id = done["execution_ids"][0]
    url = f"{stack.base}/executions/{execution_id}"
    full = (await stack.owner.get(f"{url}/report")).json()  # 14
    assert (full["totals"]["passed"], full["totals"]["failed"]) == (1, 1)
    assert (
        full["instruction"]["id"] == done["instruction_id"]
        and full["environment"]["base_url"] == DEMO
    )
    failed = next(r for r in full["results"] if r["status"] == "failed")
    bad = next(s for s in failed["steps"] if s["status"] == "failed")
    # 11: the verdict rests on an assertion with a recorded expected and found value.
    assert (
        bad["action"].startswith("assert_")
        and bad["expected"]
        and bad["actual"] == {"text": "Invalid username or password"}
    )
    assert failed["failure_category"] == "test_assertion_failure"
    # 12: evidence was captured and is served to the owner only.
    shot = next(e for e in failed["evidence"] if e["kind"] == "screenshot")
    image = await stack.owner.get(f"{url}/evidence/{shot['id']}/content")
    assert image.status_code == 200 and image.content.startswith(b"\x89PNG")
    outsider = await api.register("owner@other.io", organization="Other")
    assert (await outsider.get(f"{url}/evidence/{shot['id']}/content")).status_code == 404
    assert (await api.client().get(f"{url}/evidence/{shot['id']}/content")).status_code == 401
    # 13: the failure was analysed, and the analysis is a labelled suggestion.
    assert (
        failed["ai_suggestion"]["advisory"] is True
        and "Not a confirmed root cause" in failed["ai_suggestion"]["note"]
    )
    for fmt, start in (("pdf", b"%PDF-"), ("csv", b"\xef\xbb\xbf"), ("json", b"{")):
        exported = await stack.owner.get(f"{url}/report/export", params={"format": fmt})
        assert exported.status_code == 200 and exported.content.startswith(start), fmt
    # 15: the run is in the history, in the case's history and in the dashboard.
    history = (await stack.owner.get(f"{stack.base}/execution-history")).json()
    assert [e["id"] for e in history["items"]] == [execution_id] and history["items"][0][
        "verdict"
    ] == "failed"
    case_history = (
        await stack.owner.get(f"{stack.base}/test-cases/{failed['case_id']}/history")
    ).json()
    assert [h["status"] for h in case_history] == ["failed"]
    dashboard = (
        await stack.owner.get(f"/api/v1/organizations/{stack.owner.org_id}/dashboard")
    ).json()
    assert (
        dashboard["total_executions"],
        dashboard["results"]["passed"],
        dashboard["results"]["failed"],
    ) == (1, 1, 1)
    # And the password of the test account is in none of it.
    assert PASSWORD not in str(full) + str(report) + str(done) + str(instruction)
