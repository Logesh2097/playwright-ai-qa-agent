"""Readiness checks for the service's dependencies."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.redis import RedisClient
from app.schemas.system import DependencyCheck, ReadinessResponse

logger = logging.getLogger(__name__)

Check = Callable[[], Awaitable[None]]


def postgres_check(engine: AsyncEngine) -> Check:
    async def check() -> None:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    return check


def redis_check(redis: RedisClient) -> Check:
    async def check() -> None:
        await redis.ping()

    return check


class HealthService:
    def __init__(self, checks: Mapping[str, Check], *, timeout_s: float) -> None:
        self._checks = dict(checks)
        self._timeout_s = timeout_s

    async def readiness(self) -> ReadinessResponse:
        names = list(self._checks)
        results = await asyncio.gather(*(self._run(name) for name in names))
        checks = dict(zip(names, results, strict=True))
        ready = all(result.status == "ok" for result in results)
        return ReadinessResponse(status="ready" if ready else "not_ready", checks=checks)

    async def _run(self, name: str) -> DependencyCheck:
        started = time.perf_counter()
        error: str | None = None
        try:
            await asyncio.wait_for(self._checks[name](), timeout=self._timeout_s)
        except TimeoutError:
            error = "timeout"
        except Exception as exc:
            # Only the exception type is exposed; messages can contain hostnames or DSNs.
            error = type(exc).__name__
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        if error is not None:
            logger.warning("readiness_check_failed", extra={"dependency": name, "error": error})
            return DependencyCheck(status="error", latency_ms=latency_ms, error=error)
        return DependencyCheck(status="ok", latency_ms=latency_ms)
