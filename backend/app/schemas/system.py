"""Schemas for health, readiness and service metadata."""

from typing import Literal

from app.schemas.common import ApiModel


class LivenessResponse(ApiModel):
    status: Literal["ok"] = "ok"


class DependencyCheck(ApiModel):
    status: Literal["ok", "error"]
    latency_ms: float
    error: str | None = None


class ReadinessResponse(ApiModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, DependencyCheck]


class MetaResponse(ApiModel):
    name: str
    version: str
    environment: str
    api_version: str
