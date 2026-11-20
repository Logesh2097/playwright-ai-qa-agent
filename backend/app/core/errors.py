"""Domain exceptions. Services raise these; the API layer maps them to responses."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ErrorDetail:
    """One field-level problem inside an error response."""

    code: str
    message: str
    field: str | None = None


class AppError(Exception):
    status_code: int = 500
    code: str = "internal_error"
    title: str = "Internal server error"

    def __init__(
        self,
        detail: str | None = None,
        *,
        code: str | None = None,
        errors: Sequence[ErrorDetail] = (),
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(detail or self.title)
        self.detail = detail
        if code is not None:
            self.code = code
        self.errors = tuple(errors)
        self.headers = dict(headers) if headers else None


class BadRequestError(AppError):
    status_code = 400
    code = "bad_request"
    title = "Bad request"


class UnauthenticatedError(AppError):
    status_code = 401
    code = "unauthenticated"
    title = "Authentication required"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"
    title = "Forbidden"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"
    title = "Resource not found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"
    title = "Conflict"


class ValidationFailedError(AppError):
    status_code = 422
    code = "validation_error"
    title = "Validation failed"


class RateLimitedError(AppError):
    status_code = 429
    code = "rate_limited"
    title = "Too many requests"


class ServiceUnavailableError(AppError):
    status_code = 503
    code = "service_unavailable"
    title = "Service unavailable"
