"""The engine service around real runs: authentication, streaming, evidence, limits.

Part of `make engine-test`: each run starts a real engine process and browser.
"""

import json
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.engine.config import EngineSettings
from app.engine.server import create_app

pytestmark = pytest.mark.engine

DEMO = os.environ.get("DEMO_URL", "http://demo-app:8080")
TOKEN = "engine-test-token-0123456789abcdef"
HEADING = {"action": "assert_visible", "locator": {"by": "role", "role": "heading"}}


def plan(*steps: dict[str, Any], **more: Any) -> dict[str, Any]:
    return {
        "run_id": str(uuid.uuid4()),
        "config": more.pop("config", {}),
        "base_url": DEMO,
        "allowed_origins": [DEMO],
        "allowed_actions": ["navigate", "read"],
        "cases": [
            {
                "result_id": str(uuid.uuid4()),
                "key": "TC-1",
                "title": "t",
                "script": {"steps": list(steps)},
            }
        ],
        **more,
    }


@pytest.fixture
async def engine(tmp_path: Any) -> AsyncIterator[httpx.AsyncClient]:
    settings = EngineSettings(
        engine_token=SecretStr(TOKEN),
        engine_work_dir=str(tmp_path / "runs"),
        engine_require_proxy=False,
        engine_max_runs=1,
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://engine",
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=120,
        ) as client,
    ):
        client.app = app  # type: ignore[attr-defined]
        yield client


def events_of(response: httpx.Response) -> list[dict[str, Any]]:
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


async def test_everything_but_liveness_needs_the_token(engine: httpx.AsyncClient) -> None:
    anonymous = {"Authorization": ""}
    assert (await engine.get("/livez", headers=anonymous)).status_code == 200
    assert (await engine.get("/healthz", headers=anonymous)).status_code == 401
    assert (
        await engine.post("/runs", json=plan(HEADING), headers={"Authorization": "Bearer wrong"})
    ).status_code == 401
    assert (
        await engine.get(f"/runs/{uuid.uuid4()}/artifacts/a.png", headers=anonymous)
    ).status_code == 401
    health = (await engine.get("/healthz")).json()
    assert health["runs"] == 0 and health["playwright_version"] == "1.65.2"
    assert engine.app.state.runs == {}  # type: ignore[attr-defined]


async def test_a_run_streams_events_and_its_evidence_can_be_fetched_then_deleted(
    engine: httpx.AsyncClient,
) -> None:
    body = plan(
        {"action": "navigate", "url": "/"},
        {"action": "screenshot", "name": "home"},
        {"action": "assert_title", "title": {"equals": "Demo shop"}},
    )
    response = await engine.post("/runs", json=body)

    assert response.status_code == 200
    events = events_of(response)
    assert [e["event"] for e in events] == [
        "run_started", "case_started", "step_finished", "step_finished", "step_finished", "case_finished", "run_finished",
    ]  # fmt: skip
    assert events[-2]["status"] == "passed" and events[-1]["status"] == "completed"
    name = events[3]["artifact"]["name"]
    run_url = f"/runs/{body['run_id']}"

    image = await engine.get(f"{run_url}/artifacts/{name}")
    assert image.status_code == 200 and image.content.startswith(b"\x89PNG")
    # Only evidence files, and only by plain name.
    for other in ("engine.log", "..%2F..%2Fetc%2Fpasswd", "files", "missing.png"):
        assert (await engine.get(f"{run_url}/artifacts/{other}")).status_code == 404, other
    assert (await engine.get(f"/runs/{uuid.uuid4()}/artifacts/{name}")).status_code == 404

    assert (await engine.delete(run_url)).status_code == 204
    assert (await engine.get(f"{run_url}/artifacts/{name}")).status_code == 404
    assert (await engine.delete(run_url)).status_code == 404


async def test_invalid_plans_are_refused_before_anything_starts(engine: httpx.AsyncClient) -> None:
    for broken in (
        {**plan(HEADING), "run_id": "../../x"},
        {**plan(HEADING), "cases": []},
        plan({"action": "evaluate", "script": "1"}),
        {**plan(HEADING), "surprise": True},
    ):
        assert (await engine.post("/runs", json=broken)).status_code == 422
    assert engine.app.state.runs == {}  # type: ignore[attr-defined]


async def test_capacity_and_duplicate_runs(engine: httpx.AsyncClient) -> None:
    body = plan({"action": "navigate", "url": "/"}, HEADING)
    assert (await engine.post("/runs", json=body)).status_code == 200
    assert (await engine.post("/runs", json=body)).status_code == 409  # same run id
    # Finished runs do not take up a slot (the limit here is one).
    assert (
        await engine.post("/runs", json=plan({"action": "navigate", "url": "/"}, HEADING))
    ).status_code == 200
    assert (await engine.get("/healthz")).json()["runs"] == 0


async def test_the_run_process_gets_no_secrets_from_the_service(
    engine: httpx.AsyncClient, monkeypatch: Any
) -> None:
    """The plan carries what a run may know. The service's own token must not leak into it."""
    monkeypatch.setenv("ENGINE_TOKEN", TOKEN)
    body = plan(
        {"action": "navigate", "url": "/widgets"},
        {
            "action": "fill",
            "locator": {"by": "placeholder", "value": "Nickname"},
            "value": "{{env.ENGINE_TOKEN}}",
        },
        HEADING,
        allowed_actions=["navigate", "read", "type"],
    )
    events = events_of(await engine.post("/runs", json=body))
    step = next(e for e in events if e["event"] == "step_finished" and e["action"] == "fill")
    assert (step["status"], step["error_type"]) == ("error", "test_data")  # no such value to read
    assert TOKEN not in json.dumps(events)


async def test_a_proxy_is_required_unless_the_service_is_told_otherwise(tmp_path: Any) -> None:
    app = create_app(
        EngineSettings(engine_token=SecretStr(TOKEN), engine_work_dir=str(tmp_path / "runs"))
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://engine",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client,
    ):
        assert (await client.post("/runs", json=plan(HEADING))).status_code == 422
