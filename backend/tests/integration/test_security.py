"""Security and reliability safeguards, through the API with a real database and Redis.

Several tests here are sweeps: they walk every route the application defines,
so a route added later is covered without anyone remembering to write a test.
"""

import json
import re
import uuid
from typing import Any

import pytest
from pydantic import SecretStr

from app.models import AuditLog, TestExecution
from app.services.executions import ACTIVE_EXECUTIONS, alive_key
from app.services.progress import current_key
from app.worker import jobs as worker_jobs
from tests.conftest import Account, Api
from tests.integration.test_executions import PASSWORD_VALUE, RECIPE, SCRIPT, Rig

pytestmark = pytest.mark.integration

# Routes that are not reached with a signed-in member of an organization.
OPEN_ROUTES = (
    "/auth/",
    "/meta",
    "/webhooks/",
    "/ci/",
    "/evidence-downloads/",
    "/healthz",
    "/readyz",
    "/metrics",
)
DELETE_ACCOUNT: dict[str, Any] = {
    "steps": [
        {"action": "navigate", "url": "/account"},
        {"action": "click", "locator": {"by": "role", "role": "button", "name": "Delete account"}},
        {"action": "assert_title", "title": {"equals": "Goodbye"}},
    ]
}


def routes(api: Api) -> list[tuple[str, str]]:
    """Every (method, path) of the API that is scoped to an organization."""
    found = []
    for path, operations in api.app.openapi()["paths"].items():
        if "{organization_id}" not in path or any(part in path for part in OPEN_ROUTES):
            continue
        found += [(method.upper(), path) for method in operations]
    return sorted(found)


def fill(path: str, organization_id: str, known: dict[str, str] | None = None) -> str:
    """The path with real-looking values: known IDs where given, random ones otherwise."""
    values = {"organization_id": organization_id, **(known or {})}

    def value(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in values:
            return values[name]
        if name == "number":
            return "7"
        if name == "sha":
            return "a" * 40
        return str(uuid.uuid4())

    return re.sub(r"\{(\w+)\}", value, path)


async def viewer_in_project(api: Api, rig: Rig) -> Account:
    viewer = await api.invite_and_join(rig.owner, "viewer@acme.io", "viewer")
    await rig.owner.post(f"{rig.base}/members", json={"user_id": viewer.user_id, "role": "member"})
    return viewer


# --- tenant isolation and unauthorized actions: every route --------------------------------


async def test_no_route_answers_someone_outside_the_organization_or_without_a_session(
    api: Api,
) -> None:
    rig = await Rig(api).setup()
    case = await rig.case()
    execution = await rig.execute(case)
    outsider = await api.register("owner@other.io", organization="Other")
    anonymous = api.client()
    known = {
        "project_id": rig.ws.project_id,
        "application_id": rig.ws.application_id,
        "environment_id": rig.ws.environment_id,
        "case_id": case["id"],
        "execution_id": execution["id"],
        "result_id": execution["results"][0]["id"],
    }
    all_routes = routes(api)
    assert len(all_routes) > 100  # the sweep really covers the API

    leaked: list[tuple[Any, ...]] = []
    for method, path in all_routes:
        # Real IDs of the other organization, so that "not found" is not just a wrong ID.
        url = fill(path, rig.owner.org_id, known)
        theirs = await outsider.client.request(method, url, headers=outsider.headers, json={})
        nobody = await anonymous.request(method, url, json={})
        if theirs.status_code != 404 or nobody.status_code != 401:
            leaked.append((method, path, theirs.status_code, nobody.status_code))
        # The outsider's own organization in the address does not open the other's resources.
        if method == "DELETE" and path == "/api/v1/organizations/{organization_id}":
            continue  # would remove the outsider's own organization and end the sweep
        crossed = await outsider.client.request(
            method, fill(path, outsider.org_id, known), headers=outsider.headers, json={}
        )
        # (Deleting one's own organization is allowed, and is not a crossing.)
        own = path == "/api/v1/organizations/{organization_id}"
        if crossed.status_code < 400 and method != "GET" and not own:
            leaked.append((method, path, "crossed", crossed.status_code))
        if crossed.status_code == 200 and any(value in crossed.text for value in known.values()):
            leaked.append((method, path, "crossed-data", crossed.status_code))
    assert leaked == []


async def test_a_viewer_can_change_nothing_through_any_route(api: Api) -> None:
    rig = await Rig(api).setup()
    case = await rig.case()
    execution = await rig.execute(case)
    viewer = await viewer_in_project(api, rig)
    known = {
        "project_id": rig.ws.project_id,
        "application_id": rig.ws.application_id,
        "environment_id": rig.ws.environment_id,
        "case_id": case["id"],
        "execution_id": execution["id"],
        "result_id": execution["results"][0]["id"],
    }
    before = {
        name: len(await rig.rows(model))
        for name, model in (("audit", AuditLog), ("executions", TestExecution))
    }

    allowed = []
    for method, path in routes(api):
        if method == "GET":
            continue
        response = await viewer.client.request(
            method, fill(path, rig.owner.org_id, known), headers=viewer.headers, json={}
        )
        # Refused for the role, not found, or a body that never got as far as being applied.
        if response.status_code not in (403, 404, 409, 422) or response.status_code >= 500:
            allowed.append((method, path, response.status_code))
    assert allowed == []
    assert (await rig.owner.get(f"{rig.base}/test-cases/{case['id']}")).json()[
        "status"
    ] == "approved"
    assert len(await rig.rows(TestExecution)) == before["executions"]


# --- credential exposure -------------------------------------------------------------------


async def test_a_stored_secret_appears_in_no_answer_log_event_report_or_export(api: Api) -> None:
    rig = await Rig(api).setup()
    created = await rig.owner.post(
        rig.ws.credentials_url,
        json={"name": "customer", "role_label": "customer", "kind": "username_password",
              "secret": {"username": "demo", "password": PASSWORD_VALUE},
              "is_test_account": True, "login_recipe": RECIPE},
    )  # fmt: skip
    assert created.status_code == 201 and PASSWORD_VALUE not in created.text
    case = await rig.case("500", {"login_as": "customer", **SCRIPT})
    rig.engine.outcomes = {case["key"]: "failed"}
    rig.engine.console = [
        {
            "at": "2026-11-19T10:00:00+00:00",
            "type": "error",
            "text": f"login failed for password={PASSWORD_VALUE}",
        }
    ]
    execution = await rig.execute(case)
    known = {
        "project_id": rig.ws.project_id,
        "application_id": rig.ws.application_id,
        "environment_id": rig.ws.environment_id,
        "case_id": case["id"],
        "execution_id": execution["id"],
        "result_id": execution["results"][0]["id"],
        "credential_id": created.json()["id"],
    }

    seen = []
    for method, path in routes(api):
        if method != "GET":
            continue
        response = await rig.owner.client.get(
            fill(path, rig.owner.org_id, known), headers=rig.owner.headers
        )
        if PASSWORD_VALUE.encode() in response.content:
            seen.append(path)
    for fmt in ("json", "csv", "pdf"):
        exported = await rig.owner.get(
            f"{rig.base}/executions/{execution['id']}/report/export", params={"format": fmt}
        )
        if PASSWORD_VALUE.encode() in exported.content:
            seen.append(f"export:{fmt}")
    for item in (await rig.owner.get(f"{rig.base}/executions/{execution['id']}/evidence")).json():
        content = await rig.owner.get(
            f"{rig.base}/executions/{execution['id']}/evidence/{item['id']}/content"
        )
        if PASSWORD_VALUE.encode() in content.content:
            seen.append(f"evidence:{item['kind']}")
    assert seen == []
    assert PASSWORD_VALUE not in json.dumps(
        [r.changes for r in await rig.rows(AuditLog)], default=str
    )
    # The engine was given it (that is the point), and the log file was redacted on the way in.
    assert rig.engine.plans[0].credentials["customer"].fields["password"] == PASSWORD_VALUE
    kinds = [
        e["kind"]
        for e in (await rig.owner.get(f"{rig.base}/executions/{execution['id']}/evidence")).json()
    ]
    assert "console_log" in kinds


# --- sensitive operations -------------------------------------------------------------------


async def test_a_sensitive_step_runs_only_after_someone_entitled_approves_it_for_that_execution(
    api: Api,
) -> None:
    rig = await Rig(api).setup()
    risky, harmless = await rig.case("500", DELETE_ACCOUNT), await rig.case("510")
    developer = await api.invite_and_join(rig.owner, "dev@acme.io", "developer")
    await rig.owner.post(
        f"{rig.base}/members", json={"user_id": developer.user_id, "role": "member"}
    )

    asked = await rig.start(case_ids=[risky["id"], harmless["id"]])
    assert (asked.status_code, asked.json()["code"]) == (409, "sensitive_approval_required")
    assert asked.json()["errors"] == [
        {
            "field": risky["key"],
            "code": "delete",
            "message": "script step 2 deletes records: Delete account",
        }
    ]
    assert await rig.rows(TestExecution) == [] and api.queue.pending == []

    # A developer may run tests but not approve a sensitive step.
    refused = await rig.start(developer, case_ids=[risky["id"]], approve_sensitive_actions=True)
    assert refused.status_code == 403
    assert (await rig.start(developer, case_ids=[harmless["id"]])).status_code == 202

    approved = await rig.start(case_ids=[risky["id"]], approve_sensitive_actions=True)
    assert (
        approved.status_code == 202
        and approved.json()["config"]["sensitive_actions_approved"] is True
    )
    record = [r for r in await rig.rows(AuditLog) if r.action == "execution.start"][-1]
    assert record.changes["sensitive_actions_approved"] == [
        {"case": risky["key"], "kind": "delete", "where": "script step 2", "what": "Delete account"}
    ]
    # Approval is for one execution: the next one asks again.
    assert (await rig.start(case_ids=[risky["id"]])).status_code == 409
    # Asking for approval when nothing needs it changes nothing.
    plain = await rig.start(case_ids=[harmless["id"]], approve_sensitive_actions=True)
    assert plain.json()["config"]["sensitive_actions_approved"] is False


async def test_sensitive_steps_never_run_against_production_and_never_on_an_automated_trigger(
    api: Api,
) -> None:
    rig = await Rig(api).setup()
    risky, harmless = await rig.case("500", DELETE_ACCOUNT), await rig.case("510")
    key = (
        await rig.owner.post(
            f"/api/v1/organizations/{rig.owner.org_id}/api-keys",
            json={"name": "pipeline", "scopes": ["executions:write", "executions:read"]},
        )
    ).json()["key"]

    started = await api.client().post(
        "/api/v1/ci/executions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "project_id": rig.ws.project_id,
            "environment_id": rig.ws.environment_id,
            "case_ids": [risky["id"], harmless["id"]],
        },
    )
    assert started.status_code == 202, started.text
    await api.run_jobs()
    by_ci = await rig.get(started.json()["id"])
    statuses = {r["case_key"]: (r["status"], r["failure_summary"]) for r in by_ci["results"]}
    assert (
        statuses[risky["key"]][0] == "blocked"
        and "Needs a person's approval" in statuses[risky["key"]][1]
    )
    assert statuses[harmless["key"]][0] == "passed"
    assert [c.key for c in rig.engine.plans[-1].cases] == [harmless["key"]]
    assert by_ci["verdict"] == "inconclusive"  # a blocked case is not a pass

    # Production: not even with approval, not even for the owner.
    production = await rig.owner.create_environment(
        rig.ws.project_id, rig.ws.application_id, name="production", kind="production",
        base_url="https://prod.acme.io", is_read_only=False, requires_approval=False,
    )  # fmt: skip
    prod_case = await rig.owner.post(
        f"{rig.base}/test-cases",
        json={"application_id": rig.ws.application_id, "environment_id": production["id"],
              "content": (await rig.owner.get(f"{rig.base}/test-cases/{risky['id']}")).json()["content"]},
    )  # fmt: skip
    assert prod_case.status_code == 201, prod_case.text
    saved = await rig.owner.put(
        f"{rig.base}/test-cases/{prod_case.json()['id']}/automation", json=DELETE_ACCOUNT
    )
    assert saved.status_code == 200, saved.text
    await rig.owner.post(f"{rig.base}/test-cases/{prod_case.json()['id']}/approve")
    on_production = await rig.start(
        environment_id=production["id"],
        case_ids=[prod_case.json()["id"]],
        approve_sensitive_actions=True,
    )
    assert on_production.status_code == 202, on_production.text
    blocked = on_production.json()["results"][0]
    assert (
        blocked["status"] == "blocked"
        and "never run against a production environment" in blocked["failure_summary"]
    )
    assert on_production.json()["verdict"] == "inconclusive" and len(rig.engine.plans) == 1


# --- headers, size limits, metrics -----------------------------------------------------------


async def test_every_api_answer_is_hardened_and_oversized_bodies_are_refused(api: Api) -> None:
    owner = await api.register("owner@acme.io")
    answers = [
        await owner.get("/api/v1/auth/me"),
        await api.client().get("/api/v1/auth/me"),  # 401
        await owner.get(f"/api/v1/organizations/{uuid.uuid4()}"),  # 404
        await owner.post(owner.projects_url, json={}),  # 422
        await api.client().get("/healthz"),
    ]
    for response in answers:
        assert response.headers["x-content-type-options"] == "nosniff", response.status_code
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert response.headers["cache-control"] == "no-store"
        assert "server-timing" not in response.headers and "x-powered-by" not in response.headers

    api.app.state.settings = api.app.state.settings.model_copy(
        update={"max_request_body_bytes": 64 * 1024}
    )
    limit = api.app.state.settings.max_request_body_bytes
    # The limit was fixed when the application was built: use the real one.
    built = 4 * 1024 * 1024
    huge = await owner.client.post(
        owner.projects_url,
        content=b'{"name":"' + b"x" * (built + 10) + b'"}',
        headers={**owner.headers, "Content-Type": "application/json"},
    )
    assert (huge.status_code, huge.json()["code"]) == (413, "payload_too_large") and limit < built
    assert (await owner.get(owner.projects_url)).json() == []
    # Lists cannot be asked for without bound.
    project = await owner.create_project()
    for path in (
        "execution-history?limit=100000",
        "failures?limit=100000",
        "trends?days=100000",
        "stability?window=100000",
    ):
        assert (
            await owner.get(f"{owner.projects_url}/{project['id']}/{path}")
        ).status_code == 422, path


async def test_metrics_need_the_token_and_carry_no_identifiers(api: Api) -> None:
    from app.core.metrics import metrics

    metrics.bind(api.app.state.redis, process="api")
    rig = await Rig(api).setup()
    case = await rig.case()
    rig.engine.outcomes = {case["key"]: "failed"}
    execution = await rig.execute(case)
    client = api.client()

    assert (await client.get("/metrics")).status_code == 404  # no token configured: no endpoint
    token = "m" * 32
    api.app.state.settings = api.app.state.settings.model_copy(
        update={"metrics_token": SecretStr(token)}
    )
    assert (await client.get("/metrics")).status_code == 404
    assert (
        await client.get("/metrics", headers={"Authorization": "Bearer wrong"})
    ).status_code == 404
    assert (
        await client.get("/metrics", headers={"Authorization": f"Bearer {rig.owner.token}"})
    ).status_code == 404

    scraped = await client.get("/metrics", headers={"Authorization": f"Bearer {token}"})
    assert scraped.status_code == 200 and scraped.headers["content-type"].startswith("text/plain")
    text = scraped.text
    assert 'qa_executions_total{status="completed",verdict="failed"} 1' in text
    assert 'qa_test_results_total{status="failed"} 1' in text
    assert (
        "qa_execution_duration_seconds_count 1" in text
        and "qa_execution_queue_delay_seconds_count 1" in text
    )
    assert 'route="/organizations/{organization_id}/projects/{project_id}/executions"' in text
    assert 'qa_process_resident_memory_bytes{process="api"}' in text
    # Route templates only: no organization, project, execution or case appears.
    for identifier in (
        rig.owner.org_id,
        rig.ws.project_id,
        execution["id"],
        case["id"],
        rig.owner.email,
    ):
        assert identifier not in text


# --- reliability: recovery, cleanup, service health ------------------------------------------


def worker_context(api: Api, rig: Rig) -> dict[str, Any]:
    return {
        "session_factory": api.app.state.session_factory,
        "settings": api.app.state.settings,
        "app_redis": api.app.state.redis,
        "credential_cipher": api.app.state.credential_cipher,
        "engine_client": rig.engine,
        "egress_client": api.egress,
        "browser_manager": None,
    }


async def test_a_finished_execution_leaves_nothing_behind(api: Api) -> None:
    rig = await Rig(api).setup()
    case = await rig.case()
    rig.engine.outcomes = {case["key"]: "failed"}
    execution = await rig.execute(case)
    redis = api.app.state.redis

    assert (
        rig.engine.deleted == [execution["id"]] and rig.engine.stream_closed
    )  # the browser run is gone
    assert rig.infra.registered == set()  # and so is its way out to the network
    assert await redis.smembers(ACTIVE_EXECUTIONS) == set()
    assert not await redis.exists(alive_key(execution["id"]), current_key(execution["id"]))
    assert not await redis.exists(f"execution:cancel:{execution['id']}")
    assert (await rig.owner.get(f"{rig.base}/executions/{execution['id']}/live")).json()[
        "current"
    ] is None


async def test_an_execution_whose_worker_died_is_closed_and_not_run_again(api: Api) -> None:
    rig = await Rig(api).setup()
    first, second = await rig.case("500"), await rig.case("510")
    started = await rig.start(case_ids=[first["id"], second["id"]])
    execution_id = started.json()["id"]
    redis = api.app.state.redis
    # The worker took the job, started the first case, and then vanished: no clean-up ran.
    api.queue.pending.clear()
    from sqlalchemy import update

    from app.db.context import set_current_org
    from app.models import TestResult

    async with api.app.state.session_factory() as session, session.begin():
        await set_current_org(session, uuid.UUID(rig.owner.org_id))
        await session.execute(
            update(TestExecution)
            .where(TestExecution.id == uuid.UUID(execution_id))
            .values(status="running")
        )
        await session.execute(
            update(TestResult)
            .where(TestResult.test_case_id == uuid.UUID(first["id"]))
            .values(status="running")
        )
    member = f"{rig.owner.org_id}:{execution_id}"
    await redis.sadd(ACTIVE_EXECUTIONS, member)
    context = worker_context(api, rig)

    # While the worker shows signs of life, nothing is touched.
    await redis.set(alive_key(execution_id), "1", ex=60)
    assert await worker_jobs.recover_executions(context) == 0
    assert (await rig.get(execution_id))["status"] == "running"

    await redis.delete(alive_key(execution_id))
    assert await worker_jobs.recover_executions(context) == 1

    closed = await rig.get(execution_id)
    assert (closed["status"], closed["verdict"], closed["error"]["code"]) == (
        "failed",
        "error",
        "worker_interrupted",
    )
    by_key = {r["case_key"]: r for r in closed["results"]}
    assert (by_key[first["key"]]["status"], by_key[first["key"]]["failure_category"]) == (
        "error",
        "browser_infrastructure_failure",
    )
    assert by_key[second["key"]]["status"] == "skipped"
    assert rig.engine.plans == [] and rig.engine.deleted == [execution_id]  # cleaned up, not re-run
    assert await redis.smembers(ACTIVE_EXECUTIONS) == set()
    assert await worker_jobs.recover_executions(context) == 0  # once
    # A job delivered a second time for an execution that already ended does nothing.
    api.queue.pending.append(
        ("run_execution", {"organization_id": rig.owner.org_id, "execution_id": execution_id})
    )
    assert await api.run_jobs() == ["discarded"]
    assert (await rig.get(execution_id))["status"] == "failed"


async def test_the_worker_reports_the_health_of_the_browser_services(api: Api) -> None:
    rig = await Rig(api).setup()
    context = worker_context(api, rig)

    assert await worker_jobs.check_services(context) == {
        "engine": "ok",
        "mcp_runner": "not_configured",
    }
    seen = (await rig.owner.get("/api/v1/system/services")).json()["services"]
    assert seen["engine"]["status"] == "ok" and seen["engine"]["checked_at"]
    assert seen["mcp_runner"]["status"] == "not_configured"

    class Down:
        async def health(self) -> dict[str, Any]:
            raise ConnectionError(
                "engine refused the connection at http://engine:8902 with token abc"
            )

    assert (await worker_jobs.check_services({**context, "engine_client": Down()}))[
        "engine"
    ] == "unavailable"
    engine = (await rig.owner.get("/api/v1/system/services")).json()["services"]["engine"]
    # The kind of failure is recorded, not its text.
    assert (engine["status"], engine["detail"]) == ("unavailable", "ConnectionError")
    assert (await api.client().get("/api/v1/system/services")).status_code == 401
