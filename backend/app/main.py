"""FastAPI application factory."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agent.llm.factory import build_gateway
from app.api import health
from app.api.exception_handlers import register_exception_handlers
from app.api.v1.router import api_v1_router
from app.core.config import Settings, get_settings
from app.core.jobs import ArqJobQueue
from app.core.logging import configure_logging
from app.core.metrics import metrics
from app.core.redis import create_redis
from app.core.security.crypto import CredentialCipher, parse_master_keys
from app.core.security.url_safety import AccessValidator
from app.db.session import create_engine, create_session_factory
from app.middleware.http_security import HttpSecurityMiddleware
from app.middleware.request_context import REQUEST_ID_HEADER, RequestContextMiddleware
from app.schemas.common import ProblemDetail
from app.services.email import build_email_sender

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    engine = create_engine(settings)
    redis = create_redis(settings)
    app.state.db_engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.redis = redis
    metrics.bind(redis, process="api")
    logger.info("startup", extra={"environment": settings.app_env, "version": settings.app_version})
    try:
        yield
    finally:
        if isinstance(app.state.job_queue, ArqJobQueue):
            await app.state.job_queue.close()
        await redis.aclose()
        await engine.dispose()
        logger.info("shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    docs_enabled = not settings.is_production
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
        docs_url="/api/docs" if docs_enabled else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if docs_enabled else None,
        responses={"default": {"model": ProblemDetail, "description": "Error"}},
    )
    app.state.settings = settings
    app.state.email_sender = build_email_sender(settings)
    app.state.credential_cipher = CredentialCipher(
        parse_master_keys(settings.credential_master_keys.get_secret_value())
    )
    app.state.access_validator = AccessValidator()
    app.state.access_probe_transport = None  # tests substitute a fake network here
    app.state.llm_gateway = build_gateway(settings)
    app.state.job_queue = ArqJobQueue(settings.redis_url)

    # Innermost of the three: sees the matched route, adds headers to every answer.
    app.add_middleware(
        HttpSecurityMiddleware,
        max_body_bytes=settings.max_request_body_bytes,
        # Only where the service is reached over TLS; a browser would otherwise refuse plain http.
        hsts=settings.is_production,
    )
    # Added first, so it runs inside CORS: error responses it produces still get CORS headers.
    app.add_middleware(RequestContextMiddleware)
    if settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allowed_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", REQUEST_ID_HEADER],
            expose_headers=[REQUEST_ID_HEADER],
            max_age=600,
        )

    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(api_v1_router, prefix=settings.api_v1_prefix)
    return app
