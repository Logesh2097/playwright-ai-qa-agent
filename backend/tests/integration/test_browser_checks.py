"""Browser checks through the API, with the database and Redis real and the browser mocked.

The mock is a real MCP server (see `tests/mcp_fakes.py`), so the worker code
runs the genuine client, permission and session layers.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app.db.context import set_current_org
from app.models import AgentExecutionLog, AuditLog
from tests.conftest import Account, Api, Workspace
from tests.mcp_fakes import FakeBrowser, FakeInfrastructure, MockMcpServer

pytestmark = pytest.mark.integration

BASE = "https://staging.acme.io"
USERNAME = "demo"
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
    {"action": "expect", "text": "Signed in as", "url_path": "/account"},
]


class Rig:
    def __init__(self, api: Api, **settings: Any) -> None:
        self.api = api
        self.browser = FakeBrowser(username=USERNAME, password=PASSWORD_VALUE, base=BASE)
        self.server = MockMcpServer(self.browser)
        self.infra = FakeInfrastructure()
        api.browser_manager = self.infra.manager(self.server)
        api.app.state.settings = api.app.state.settings.model_copy(
            update={"runner_url": "http://runner:8900", **settings}
        )


async def _workspace(api: Api, **environment: Any) -> tuple[Account, Workspace]:
    owner = await api.register("owner@acme.io")
    environment.setdefault("is_read_only", False)
    environment.setdefault("requires_approval", False)
    return owner, await owner.setup_environment(**environment)


async def _credential(account: Account, ws: Workspace, **overrides: Any) -> str:
    body: dict[str, Any] = {
        "name": "customer",
        "role_label": "customer",
        "kind": "username_password",
        "secret": {"username": USERNAME, "password": PASSWORD_VALUE},
        "is_test_account": True,
        "login_recipe": RECIPE,
    }
    body.update(overrides)
    response = await account.post(ws.credentials_url, json=body)
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _checks(ws: Workspace) -> str:
    return f"{ws.environment_url}/browser-checks"


async def _run(
    api: Api, account: Account, ws: Workspace, credential_id: str | None = None
) -> dict[str, Any]:
    started = await account.post(_checks(ws), json={"credential_id": credential_id})
    assert started.status_code == 202, started.text
    assert started.json()["status"] == "queued"
    await api.run_jobs()
    finished = await account.get(f"{_checks(ws)}/{started.json()['id']}")
    assert finished.status_code == 200, finished.text
    body: dict[str, Any] = finished.json()
    return body


async def _rows(api: Api, org_id: str, model: Any) -> list[Any]:
    async with api.app.state.session_factory() as session, session.begin():
        await set_current_org(session, uuid.UUID(org_id))
        return list((await session.execute(select(model))).scalars())


# --- the verdict --------------------------------------------------------------------------


async def test_check_without_credential_passes_once_the_page_really_loaded(api: Api) -> None:
    rig = Rig(api)
    owner, ws = await _workspace(api)

    check = await _run(api, owner, ws)

    assert check["status"] == "passed" and check["error"] is None
    assert check["result"]["page_url"] == BASE
    assert check["result"]["logged_in"] is None
    assert check["result"]["actions_performed"] == 1
    assert check["session"]["server"] == "Playwright"
    # Only what this workflow needs was offered, out of everything the server has.
    assert check["session"]["tools_exposed"] == ["browser_navigate", "browser_snapshot"]
    assert check["session"]["tools_withheld"]["browser_evaluate"] == "tool_not_requested"
    assert [c["tool"] for c in check["tool_calls"]] == ["browser_navigate"]
    assert not rig.infra.running and not rig.infra.registered


async def test_login_check_passes_only_after_every_recipe_step_and_expectation(api: Api) -> None:
    rig = Rig(api)
    owner, ws = await _workspace(api)
    credential_id = await _credential(owner, ws)

    check = await _run(api, owner, ws, credential_id)

    assert check["status"] == "passed"
    assert check["result"]["logged_in"] is True
    assert check["result"]["login_steps_performed"] == 5
    assert check["result"]["page_url"] == f"{BASE}/account"
    calls = check["tool_calls"]
    assert [c["tool"] for c in calls][:5] == [
        "browser_navigate", "browser_navigate", "browser_type", "browser_type", "browser_click",
    ]  # fmt: skip
    assert all(c["status"] == "ok" and c["duration_ms"] is not None for c in calls)
    assert calls[2]["category"] == "state_changing"
    assert rig.browser.signed_in


async def test_password_is_in_no_response_log_row_or_audit_entry(
    api: Api, caplog: pytest.LogCaptureFixture
) -> None:
    rig = Rig(api)
    rig.browser.echo_typed_text = True
    owner, ws = await _workspace(api)
    credential_id = await _credential(owner, ws)

    with caplog.at_level("DEBUG"):
        check = await _run(api, owner, ws, credential_id)

    assert check["status"] == "passed"
    assert PASSWORD_VALUE not in str(check)
    typed = [c for c in check["tool_calls"] if c["tool"] == "browser_type"]
    assert typed[0]["arguments"]["text"] == USERNAME
    assert typed[1]["arguments"]["text"] == "[REDACTED]"
    logs = await _rows(api, owner.org_id, AgentExecutionLog)
    assert len(logs) >= 5
    assert PASSWORD_VALUE not in str([(r.tool_args, r.tool_result_summary, r.error) for r in logs])
    audit = await _rows(api, owner.org_id, AuditLog)
    assert PASSWORD_VALUE not in str([r.changes for r in audit])
    assert PASSWORD_VALUE not in caplog.text
    # The use of the credential itself is on record.
    used = [r for r in audit if r.action == "credential.use"]
    assert len(used) == 1 and used[0].changes["purpose"] == "browser_check"
    assert used[0].actor_type == "system" and str(used[0].actor_id) == owner.user_id


async def test_wrong_password_fails_the_check(api: Api) -> None:
    Rig(api)
    owner, ws = await _workspace(api)
    credential_id = await _credential(
        owner, ws, secret={"username": USERNAME, "password": "not-the-password"}
    )

    check = await _run(api, owner, ws, credential_id)

    assert check["status"] == "failed"
    assert check["result"] is None
    assert check["error"]["code"] == "login_failed"
    assert "not-the-password" not in str(check)


async def test_check_fails_when_the_browser_is_redirected_off_the_allowed_origins(api: Api) -> None:
    rig = Rig(api)
    rig.browser.redirect_to[BASE] = "https://elsewhere.example/landing"
    owner, ws = await _workspace(api)

    check = await _run(api, owner, ws)

    assert check["status"] == "failed"
    assert check["error"]["code"] == "left_allowed_origins"
    assert check["tool_calls"][0]["status"] == "blocked"


async def test_check_fails_when_the_server_does_not_say_which_page_loaded(api: Api) -> None:
    """An answer nobody can interpret is not evidence that the page opened."""
    from tests.mcp_fakes import text_result

    rig = Rig(api)
    rig.browser.scripted["browser_navigate"] = [text_result("Navigation finished, probably.")]
    owner, ws = await _workspace(api)

    check = await _run(api, owner, ws)

    assert (check["status"], check["error"]["code"]) == ("failed", "page_unknown")


@pytest.mark.parametrize(
    ("break_it", "code"),
    [
        (lambda rig: setattr(rig.infra, "runner_status", 500), "runner_unavailable"),
        (lambda rig: setattr(rig.infra, "runner_status", 429), "runner_busy"),
        (
            lambda rig: setattr(rig.server, "fail_connect", ConnectionRefusedError()),
            "mcp_server_unavailable",
        ),
        (lambda rig: setattr(rig.server, "fail_discovery", True), "mcp_discovery_failed"),
    ],
)
async def test_infrastructure_failures_fail_the_check_and_mark_the_connection(
    api: Api, break_it: Any, code: str
) -> None:
    rig = Rig(api)
    break_it(rig)
    owner, ws = await _workspace(api)

    check = await _run(api, owner, ws)

    assert (check["status"], check["error"]["code"]) == ("failed", code)
    assert not rig.infra.running and not rig.infra.registered
    status = (await owner.get(f"/api/v1/organizations/{owner.org_id}/browser")).json()
    assert status["connection"]["status"] == "unhealthy"
    assert status["connection"]["last_error"].startswith(code)


async def test_worker_without_browser_services_fails_the_check(api: Api) -> None:
    Rig(api)
    api.browser_manager = None
    owner, ws = await _workspace(api)

    check = await _run(api, owner, ws)

    assert (check["status"], check["error"]["code"]) == ("failed", "browser_not_configured")


async def test_a_job_whose_check_is_gone_does_nothing(api: Api) -> None:
    rig = Rig(api)
    owner, _ = await _workspace(api)
    await api.queue.enqueue(
        "run_browser_check",
        organization_id=owner.org_id,
        check_id=str(uuid.uuid4()),
        state_changes_approved=True,
    )
    assert await api.run_jobs() == ["discarded"]
    assert rig.infra.requests == []


# --- who may run what ---------------------------------------------------------------------


async def test_not_configured_installations_answer_503(api: Api) -> None:
    owner, ws = await _workspace(api)
    response = await owner.post(_checks(ws), json={})
    assert (response.status_code, response.json()["code"]) == (503, "browser_not_configured")
    status = await owner.get(f"/api/v1/organizations/{owner.org_id}/browser")
    assert status.json()["configured"] is False


async def test_viewers_cannot_start_checks_and_other_organizations_see_nothing(api: Api) -> None:
    Rig(api)
    owner, ws = await _workspace(api)
    viewer = await api.invite_and_join(owner, "viewer@acme.io", "viewer")
    await owner.post(
        f"{ws.project_url}/members", json={"user_id": viewer.user_id, "role": "member"}
    )
    outsider = await api.register("owner@other.io", organization="Other")
    check = await _run(api, owner, ws)

    assert (await viewer.post(_checks(ws), json={})).status_code == 403
    assert (await outsider.post(_checks(ws), json={})).status_code == 404
    assert (await outsider.get(f"{_checks(ws)}/{check['id']}")).status_code == 404
    assert api.queue.pending == []

    # A check is only readable under the environment it was run for.
    other_env = await owner.create_environment(
        ws.project_id,
        ws.application_id,
        name="dev",
        kind="development",
        base_url="https://auth.acme.io",
    )
    elsewhere = f"{ws.application_url}/environments/{other_env['id']}/browser-checks/{check['id']}"
    assert (await owner.get(elsewhere)).status_code == 404
    assert (await owner.get(f"{_checks(ws)}/{uuid.uuid4()}")).status_code == 404


async def test_read_only_environment_can_be_opened_but_not_signed_in_to(api: Api) -> None:
    rig = Rig(api)
    owner, ws = await _workspace(api, is_read_only=True)
    credential_id = await _credential(owner, ws)

    refused = await owner.post(_checks(ws), json={"credential_id": credential_id})
    assert (refused.status_code, refused.json()["code"]) == (409, "environment_read_only")
    assert api.queue.pending == []

    looked = await _run(api, owner, ws)
    assert looked["status"] == "passed"
    assert rig.browser.names() == ["browser_navigate", "browser_close"]


async def test_login_on_an_approval_environment_needs_an_admin_and_is_audited(api: Api) -> None:
    rig = Rig(api)
    owner, ws = await _workspace(api, requires_approval=True)
    engineer = await api.invite_and_join(owner, "qa@acme.io", "qa_engineer")
    await owner.post(
        f"{ws.project_url}/members", json={"user_id": engineer.user_id, "role": "member"}
    )
    credential_id = await _credential(owner, ws)

    refused = await engineer.post(_checks(ws), json={"credential_id": credential_id})
    assert (refused.status_code, refused.json()["code"]) == (403, "approval_required")
    assert api.queue.pending == [] and rig.browser.calls == []

    approved = await _run(api, owner, ws, credential_id)
    assert approved["status"] == "passed"
    started = [
        r for r in await _rows(api, owner.org_id, AuditLog) if r.action == "browser_check.start"
    ]
    assert started[-1].changes["state_changes_approved"] is True


async def test_approval_cannot_be_forged_by_whoever_enqueues_without_it(api: Api) -> None:
    """The worker enforces the approval flag itself: without it, typing is refused."""
    rig = Rig(api)
    owner, ws = await _workspace(api, requires_approval=True)
    credential_id = await _credential(owner, ws)
    started = await owner.post(_checks(ws), json={"credential_id": credential_id})
    job, kwargs = api.queue.pending[0]
    api.queue.pending[0] = (job, {**kwargs, "state_changes_approved": False})

    await api.run_jobs()

    check = (await owner.get(f"{_checks(ws)}/{started.json()['id']}")).json()
    assert (check["status"], check["error"]["code"]) == ("failed", "approval_required")
    assert "browser_type" not in rig.browser.names() and not rig.browser.signed_in


async def test_credential_must_be_usable_and_belong_to_the_environment(api: Api) -> None:
    Rig(api)
    owner, ws = await _workspace(api)
    no_recipe = await _credential(owner, ws, name="bare", login_recipe=[])
    by_reference = await _credential(
        owner, ws, name="ref", secret=None, external_ref="env:STAGING_PASSWORD"
    )

    for credential_id, status, code in (
        (no_recipe, 409, "login_recipe_missing"),
        (by_reference, 409, "credential_not_stored"),
        (str(uuid.uuid4()), 404, "not_found"),
    ):
        response = await owner.post(_checks(ws), json={"credential_id": credential_id})
        assert (response.status_code, response.json()["code"]) == (status, code)
    assert api.queue.pending == []


async def test_checks_are_rate_limited_per_organization(api: Api) -> None:
    Rig(api, browser_check_limit=2)
    owner, ws = await _workspace(api)
    assert (await owner.post(_checks(ws), json={})).status_code == 202
    assert (await owner.post(_checks(ws), json={})).status_code == 202
    limited = await owner.post(_checks(ws), json={})
    assert limited.status_code == 429 and "Retry-After" in limited.headers


# --- status and tool administration -------------------------------------------------------


async def test_status_lists_every_tool_with_its_classification_and_what_was_discovered(
    api: Api,
) -> None:
    Rig(api)
    owner, ws = await _workspace(api)
    url = f"/api/v1/organizations/{owner.org_id}/browser"

    before = (await owner.get(url)).json()
    assert before["configured"] is True and before["connection"] is None
    tools = {t["name"]: t for t in before["tools"]}
    assert tools["browser_snapshot"]["category"] == "read_only"
    assert tools["browser_click"]["category"] == "state_changing"
    assert tools["browser_evaluate"]["availability"] == "forbidden"
    assert tools["browser_network_request"]["availability"] == "needs_approval"
    assert tools["browser_close"]["availability"] == "internal"
    assert "filename" not in tools["browser_snapshot"]["arguments"]
    assert tools["browser_click"]["discovered"] is None

    await _run(api, owner, ws)

    after = (await owner.get(url)).json()
    assert after["connection"]["status"] == "active"
    assert after["connection"]["server_version"].startswith("Playwright ")
    assert after["connection"]["unknown_tools"] == ["browser_teleport"]
    assert {t["name"]: t for t in after["tools"]}["browser_click"]["discovered"] is True


async def test_admin_can_switch_tools_off_but_never_enable_forbidden_ones(api: Api) -> None:
    rig = Rig(api)
    owner, ws = await _workspace(api)
    engineer = await api.invite_and_join(owner, "qa@acme.io", "qa_engineer")
    credential_id = await _credential(owner, ws)
    url = f"/api/v1/organizations/{owner.org_id}/browser/allowed-tools"

    assert (
        await engineer.put(url, json={"allowed_tools": ["browser_navigate"]})
    ).status_code == 403
    for forbidden in (["browser_evaluate"], ["browser_close"], ["browser_teleport"], []):
        response = await owner.put(url, json={"allowed_tools": forbidden})
        assert response.status_code == 422, forbidden

    restricted = await owner.put(
        url, json={"allowed_tools": ["browser_navigate", "browser_snapshot"]}
    )
    assert restricted.status_code == 200 and restricted.json()["restricted"] is True
    tools = {t["name"]: t["enabled"] for t in restricted.json()["tools"]}
    assert tools["browser_navigate"] and not tools["browser_type"]

    # With typing switched off the login cannot run, so the check must not pass.
    blocked = await _run(api, owner, ws, credential_id)
    assert (blocked["status"], blocked["error"]["code"]) == ("failed", "tool_disabled")
    assert "browser_type" not in rig.browser.names()

    lifted = await owner.put(url, json={"allowed_tools": None})
    assert lifted.json()["restricted"] is False
    assert (await _run(api, owner, ws, credential_id))["status"] == "passed"
    actions = [r.action for r in await _rows(api, owner.org_id, AuditLog)]
    assert actions.count("browser.tools.update") == 2
