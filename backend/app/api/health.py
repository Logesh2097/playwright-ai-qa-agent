"""Liveness and readiness probes. Unversioned: they are an operational contract."""

import hmac
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.deps import CurrentUserDep, HealthServiceDep, RedisDep, SettingsDep
from app.core.errors import NotFoundError
from app.core.metrics import metrics
from app.schemas.system import LivenessResponse, ReadinessResponse

router = APIRouter(tags=["health"])


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> LivenessResponse:
    """The process is up. Does not touch any dependency."""
    return LivenessResponse()


@router.get(
    "/readyz",
    summary="Readiness probe",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def readyz(health: HealthServiceDep, response: Response) -> ReadinessResponse:
    """PostgreSQL and Redis are reachable. Returns 503 with the same body when not."""
    report = await health.readiness()
    if report.status != "ready":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return report


_bearer = HTTPBearer(auto_error=False)
SERVICE_HEALTH = "qa:health"


@router.get(
    "/metrics", summary="Operational metrics in Prometheus text format", include_in_schema=False
)
async def get_metrics(
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Response:
    """Needs the installation's metrics token. Without one configured there is no such endpoint."""
    expected = settings.metrics_token.get_secret_value() if settings.metrics_token else None
    presented = credentials.credentials if credentials else ""
    if expected is None or not hmac.compare_digest(presented.encode(), expected.encode()):
        raise NotFoundError("Not found.")
    return Response(await metrics.render(), media_type="text/plain; version=0.0.4")


@router.get(
    "/api/v1/system/services", summary="Health of the browser services, as the worker last saw it"
)
async def service_health(_: CurrentUserDep, redis: RedisDep) -> dict[str, Any]:
    """The API cannot reach the test engine or the browser runner; the worker checks them."""
    raw: Any = await redis.hgetall(SERVICE_HEALTH)  # type: ignore[misc]
    services = {}
    for name, value in raw.items():
        key = name.decode() if isinstance(name, bytes) else str(name)
        services[key] = json.loads(value)
    return {"services": services}
