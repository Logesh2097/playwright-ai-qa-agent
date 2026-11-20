from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from pydantic import BaseModel, Field

from app.core.config import Settings
from app.core.errors import ConflictError, ErrorDetail, NotFoundError
from app.main import create_app

router = APIRouter(prefix="/_test")


class Payload(BaseModel):
    name: str = Field(min_length=3)
    password: str


@router.get("/not-found")
async def raise_not_found() -> None:
    raise NotFoundError("Project does not exist")


@router.get("/conflict")
async def raise_conflict() -> None:
    raise ConflictError(
        code="duplicate",
        errors=[ErrorDetail(field="slug", code="taken", message="already in use")],
        headers={"Retry-After": "1"},
    )


@router.get("/boom")
async def raise_unexpected() -> None:
    raise RuntimeError("database password is hunter2")


@router.post("/validate")
async def validate(payload: Payload) -> dict[str, str]:
    return {"name": payload.name}


@pytest.fixture
async def error_client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app: FastAPI = create_app(settings)
    app.include_router(router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def assert_problem(response: httpx.Response, status: int, code: str) -> dict[str, object]:
    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    body: dict[str, object] = response.json()
    assert set(body) == {"type", "title", "status", "code", "detail", "errors", "request_id"}
    assert body["status"] == status
    assert body["code"] == code
    assert body["request_id"] == response.headers["x-request-id"]
    return body


async def test_app_error_uses_envelope(error_client: httpx.AsyncClient) -> None:
    body = assert_problem(await error_client.get("/_test/not-found"), 404, "not_found")
    assert body["title"] == "Resource not found"
    assert body["detail"] == "Project does not exist"
    assert body["errors"] == []


async def test_app_error_carries_code_errors_and_headers(error_client: httpx.AsyncClient) -> None:
    response = await error_client.get("/_test/conflict")
    body = assert_problem(response, 409, "duplicate")
    assert body["errors"] == [{"field": "slug", "code": "taken", "message": "already in use"}]
    assert response.headers["retry-after"] == "1"


async def test_unknown_route_uses_envelope(error_client: httpx.AsyncClient) -> None:
    body = assert_problem(await error_client.get("/nope"), 404, "not_found")
    assert body["detail"] is None


async def test_wrong_method_uses_envelope(error_client: httpx.AsyncClient) -> None:
    assert_problem(await error_client.post("/healthz"), 405, "method_not_allowed")


async def test_validation_error_lists_fields_without_echoing_input(
    error_client: httpx.AsyncClient,
) -> None:
    response = await error_client.post(
        "/_test/validate", json={"name": "ab", "password": "s3cret-value"}
    )
    body = assert_problem(response, 422, "validation_error")
    errors = body["errors"]
    assert isinstance(errors, list)
    assert errors[0]["field"] == "body.name"
    assert errors[0]["code"] == "string_too_short"
    assert "s3cret-value" not in response.text


async def test_unhandled_exception_returns_generic_500(error_client: httpx.AsyncClient) -> None:
    response = await error_client.get("/_test/boom")
    body = assert_problem(response, 500, "internal_error")
    assert body["detail"] is None
    assert "hunter2" not in response.text
    assert "RuntimeError" not in response.text
