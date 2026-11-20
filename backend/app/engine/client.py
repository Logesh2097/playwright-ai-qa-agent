"""The worker's side of the engine service."""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
from pydantic import ValidationError

from app.engine.protocol import EVENT, Event, RunPlan

MAX_ARTIFACT_BYTES = 100 * 1024 * 1024


class EngineError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class EngineClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        connect_timeout_s: float = 10.0,
        idle_timeout_s: float = 300.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._transport = transport
        # No event for this long means the engine is stuck, whatever it is doing.
        self._timeout = httpx.Timeout(connect_timeout_s, read=idle_timeout_s)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=self._timeout,
            transport=self._transport,
            trust_env=False,
        )

    async def run(self, plan: RunPlan) -> AsyncIterator[Event]:
        """Start a run and yield its events. Leaving the loop early stops the run."""
        try:
            async with (
                self._client() as client,
                client.stream(
                    "POST",
                    "/runs",
                    content=plan.model_dump_json(),
                    headers={"Content-Type": "application/json"},
                ) as response,
            ):
                if response.status_code == 429:
                    raise EngineError("engine_busy", "All test engine slots are in use.")
                if response.status_code == 422:
                    # The engine refused the plan. Say where, never what: a plan holds credentials.
                    raise EngineError("engine_rejected_plan", _where(await response.aread()))
                if response.status_code != 200:
                    raise EngineError(
                        "engine_unavailable", f"The test engine answered {response.status_code}."
                    )
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        yield EVENT.validate_python(json.loads(line))
                    except (ValueError, ValidationError):
                        raise EngineError(
                            "engine_protocol", "The test engine sent something unreadable."
                        ) from None
        except httpx.HTTPError as exc:
            raise EngineError(
                "engine_unavailable",
                f"The test engine could not be reached ({type(exc).__name__}).",
            ) from None

    async def fetch_artifact(self, run_id: str, name: str) -> bytes:
        try:
            async with self._client() as client:
                response = await client.get(f"/runs/{run_id}/artifacts/{name}")
        except httpx.HTTPError as exc:
            raise EngineError(
                "engine_unavailable",
                f"The test engine could not be reached ({type(exc).__name__}).",
            ) from None
        if response.status_code != 200:
            raise EngineError("artifact_missing", f"The engine has no file named {name}.")
        if len(response.content) > MAX_ARTIFACT_BYTES:
            raise EngineError("artifact_too_large", f"{name} is too large to store.")
        return response.content

    async def delete_run(self, run_id: str) -> None:
        try:
            async with self._client() as client:
                await client.delete(f"/runs/{run_id}")
        except httpx.HTTPError:
            pass  # the engine removes finished runs itself after a while

    async def health(self) -> dict[str, Any]:
        try:
            async with self._client() as client:
                response = await client.get("/healthz")
        except httpx.HTTPError as exc:
            raise EngineError(
                "engine_unavailable",
                f"The test engine could not be reached ({type(exc).__name__}).",
            ) from None
        if response.status_code != 200:
            raise EngineError(
                "engine_unavailable", f"The test engine answered {response.status_code}."
            )
        body = response.json()
        return body if isinstance(body, dict) else {}


def _where(body: bytes) -> str:
    """Which parts of a plan the engine refused, without any of the values in it."""
    try:
        detail = json.loads(body).get("detail")
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, str):
        return f"The test engine refused the plan: {detail[:200]}"
    places = []
    for item in detail if isinstance(detail, list) else []:
        if isinstance(item, dict):
            location = ".".join(str(part) for part in item.get("loc", [])[1:])
            places.append(f"{location}: {str(item.get('msg', ''))[:120]}")
    listed = "; ".join(places[:5]) or "no details were given"
    return f"The test engine refused the plan ({listed})."
