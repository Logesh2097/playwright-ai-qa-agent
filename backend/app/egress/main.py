"""Egress proxy process: the proxy listener plus a small authenticated admin API."""

import asyncio
import hmac
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.core.logging import configure_logging
from app.core.security.url_safety import AccessValidator, UrlNotAllowedError, parse_origin
from app.egress.config import EgressSettings
from app.egress.proxy import EgressProxy, RegistryFullError, SessionRegistry

logger = logging.getLogger(__name__)


class SessionRulesIn(BaseModel):
    secret: str = Field(min_length=16, max_length=200)
    allowed_origins: list[str] = Field(min_length=1, max_length=50)
    allow_private_network: bool = False
    allow_http: bool = True
    ttl_s: int = Field(ge=10, le=86_400)


def create_app(settings: EgressSettings | None = None) -> FastAPI:
    settings = settings or EgressSettings()
    configure_logging(settings.log_level)
    registry = SessionRegistry(max_sessions=settings.egress_max_sessions)
    proxy = EgressProxy(
        registry,
        AccessValidator(),
        deny_networks=settings.deny_networks,
        connect_timeout_s=settings.egress_connect_timeout_s,
        idle_timeout_s=settings.egress_idle_timeout_s,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        server = await proxy.serve(settings.egress_proxy_host, settings.egress_proxy_port)
        logger.info("egress_proxy_started", extra={"port": settings.egress_proxy_port})
        try:
            yield
        finally:
            server.close()
            with suppress(TimeoutError):
                await asyncio.wait_for(server.wait_closed(), timeout=5)

    app = FastAPI(
        title="egress-proxy", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.registry = registry

    def authorize(authorization: Annotated[str, Header()] = "") -> None:
        expected = f"Bearer {settings.egress_admin_token.get_secret_value()}"
        if not hmac.compare_digest(authorization.encode(), expected.encode()):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED)

    @app.get("/livez")
    async def livez() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/healthz", dependencies=[Depends(authorize)])
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "sessions": len(registry)}

    @app.put(
        "/sessions/{session_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(authorize)],
    )
    async def register(session_id: uuid.UUID, rules: SessionRulesIn, _request: Request) -> Response:
        try:
            origins = [parse_origin(o, allow_http=True) for o in rules.allowed_origins]
        except UrlNotAllowedError as exc:
            raise HTTPException(422, exc.code) from None
        try:
            registry.register(
                str(session_id),
                secret=rules.secret,
                allowed_origins=origins,
                allow_private_network=rules.allow_private_network,
                allow_http=rules.allow_http,
                ttl_s=rules.ttl_s,
            )
        except RegistryFullError:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.delete("/sessions/{session_id}", dependencies=[Depends(authorize)])
    async def unregister(session_id: uuid.UUID) -> dict[str, Any]:
        rules = registry.remove(str(session_id))
        if rules is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        return {
            "allowed": rules.allowed_count,
            "blocked_count": rules.blocked_count,
            "blocked": list(rules.blocked),
        }

    return app
