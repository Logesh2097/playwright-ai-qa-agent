import asyncio

import httpx
from fastapi import FastAPI

from app.api.deps import get_health_service
from app.services.health import HealthService


async def _ok() -> None:
    return None


async def _fail() -> None:
    raise ConnectionRefusedError("postgres://user:secret@db.internal:5432")


async def _hang() -> None:
    await asyncio.sleep(5)


async def test_healthz_is_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_ready_when_all_checks_pass(client: httpx.AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert set(body["checks"]) == {"postgres", "redis"}
    assert all(check["status"] == "ok" for check in body["checks"].values())


async def test_readyz_returns_503_when_a_dependency_fails(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    app.dependency_overrides[get_health_service] = lambda: HealthService(
        {"postgres": _fail, "redis": _ok}, timeout_s=1.0
    )
    response = await client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["postgres"]["status"] == "error"
    assert body["checks"]["redis"]["status"] == "ok"


async def test_readyz_does_not_leak_exception_messages(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    app.dependency_overrides[get_health_service] = lambda: HealthService(
        {"postgres": _fail}, timeout_s=1.0
    )
    response = await client.get("/readyz")
    assert response.json()["checks"]["postgres"]["error"] == "ConnectionRefusedError"
    assert "secret" not in response.text
    assert "db.internal" not in response.text


async def test_readiness_check_times_out() -> None:
    service = HealthService({"slow": _hang}, timeout_s=0.05)
    report = await service.readiness()
    assert report.status == "not_ready"
    assert report.checks["slow"].error == "timeout"


async def test_meta_is_served_under_versioned_prefix(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/meta")
    assert response.status_code == 200
    assert response.json() == {
        "name": "qa-agent",
        "version": "0.1.0",
        "environment": "test",
        "api_version": "v1",
    }
