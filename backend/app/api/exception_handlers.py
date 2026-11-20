"""Maps exceptions to the standard error envelope."""

import logging
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import AppError, ErrorDetail
from app.core.problem import problem_response
from app.db.errors import constraint_name, sqlstate, to_app_error

logger = logging.getLogger(__name__)

_HTTP_STATUS_CODES = {
    400: "bad_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_error",
    429: "rate_limited",
    503: "service_unavailable",
}


async def _handle_app_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)  # noqa: S101 - narrowed for the type checker
    if exc.status_code >= 500 and exc.__traceback__ is not None:
        logger.error("app_error", extra={"code": exc.code}, exc_info=exc)
    return problem_response(
        status=exc.status_code,
        code=exc.code,
        title=exc.title,
        detail=exc.detail,
        errors=exc.errors,
        headers=exc.headers,
    )


async def _handle_http_exception(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101
    try:
        title = HTTPStatus(exc.status_code).phrase
    except ValueError:
        title = "Error"
    code = _HTTP_STATUS_CODES.get(
        exc.status_code, "internal_error" if exc.status_code >= 500 else "http_error"
    )
    # Starlette defaults `detail` to the status phrase; do not repeat the title.
    detail = exc.detail if isinstance(exc.detail, str) and exc.detail != title else None
    return problem_response(
        status=exc.status_code, code=code, title=title, detail=detail, headers=exc.headers
    )


async def _handle_validation_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101
    # The submitted values are deliberately left out: they may contain secrets.
    errors = [
        ErrorDetail(
            field=".".join(str(part) for part in error.get("loc", ())) or None,
            code=str(error.get("type", "invalid")),
            message=str(error.get("msg", "Invalid value")),
        )
        for error in exc.errors()
    ]
    return problem_response(
        status=422,
        code="validation_error",
        title="Validation failed",
        detail="The request did not pass validation.",
        errors=errors,
    )


async def _handle_database_error(request: Request, exc: Exception) -> JSONResponse:
    """Database errors no service handled. The driver message is logged, never returned."""
    assert isinstance(exc, SQLAlchemyError)  # noqa: S101
    app_error = to_app_error(exc)
    logger.log(
        logging.ERROR if app_error.status_code >= 500 else logging.WARNING,
        "database_error",
        extra={
            "sqlstate": sqlstate(exc),
            "constraint": constraint_name(exc),
            "error_type": type(exc).__name__,
        },
        exc_info=exc if app_error.status_code >= 500 else None,
    )
    return await _handle_app_error(request, app_error)


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _handle_app_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(SQLAlchemyError, _handle_database_error)
    # Anything else is caught by RequestContextMiddleware and returned as a 500 envelope.
