import json
import logging

import httpx
import pytest

from app.core.logging import JsonFormatter, get_request_id, request_id_var
from tests.conftest import make_settings


async def test_request_id_is_generated(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    request_id = response.headers["x-request-id"]
    assert len(request_id) == 32
    assert request_id.isalnum()


async def test_valid_inbound_request_id_is_echoed(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz", headers={"X-Request-ID": "ci-run-2026.11.18_42"})
    assert response.headers["x-request-id"] == "ci-run-2026.11.18_42"


@pytest.mark.parametrize("bad", ["short", "x" * 65, "has space in it", "new\\nline-injection"])
async def test_malformed_inbound_request_id_is_replaced(
    client: httpx.AsyncClient, bad: str
) -> None:
    response = await client.get("/healthz", headers={"X-Request-ID": bad})
    assert response.headers["x-request-id"] != bad
    assert len(response.headers["x-request-id"]) == 32


async def test_request_id_does_not_leak_between_requests(client: httpx.AsyncClient) -> None:
    first = await client.get("/healthz")
    second = await client.get("/healthz")
    assert first.headers["x-request-id"] != second.headers["x-request-id"]
    assert get_request_id() is None


async def test_access_log_line_has_request_fields(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="app.access"):
        response = await client.get("/api/v1/meta")
    record = next(r for r in caplog.records if r.name == "app.access")
    assert record.getMessage() == "request"
    assert record.__dict__["method"] == "GET"
    assert record.__dict__["path"] == "/api/v1/meta"
    assert record.__dict__["status"] == 200
    assert response.status_code == 200


def test_json_formatter_output() -> None:
    record = logging.LogRecord("app.test", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    record.execution_id = "abc"
    token = request_id_var.set("req-12345678")
    try:
        payload = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(token)
    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"
    assert payload["request_id"] == "req-12345678"
    assert payload["execution_id"] == "abc"
    assert payload["timestamp"].endswith("+00:00")


async def test_cors_allows_only_configured_origins() -> None:
    from app.main import create_app

    app = create_app(make_settings(cors_allowed_origins="http://localhost:5173"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        allowed = await http_client.get("/healthz", headers={"Origin": "http://localhost:5173"})
        denied = await http_client.get("/healthz", headers={"Origin": "http://evil.example"})
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "x-request-id" in allowed.headers["access-control-expose-headers"].lower()
    assert "access-control-allow-origin" not in denied.headers
