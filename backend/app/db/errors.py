"""Translation of database exceptions into domain errors.

Raw driver messages contain table, column and value details, so they are
logged but never returned to clients.
"""

import logging

from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError

from app.core.errors import AppError, ConflictError, ServiceUnavailableError, ValidationFailedError

logger = logging.getLogger(__name__)

UNIQUE_VIOLATION = "23505"
FOREIGN_KEY_VIOLATION = "23503"
NOT_NULL_VIOLATION = "23502"
CHECK_VIOLATION = "23514"
INSUFFICIENT_PRIVILEGE = "42501"


def sqlstate(exc: BaseException) -> str | None:
    """PostgreSQL error code of a SQLAlchemy exception, if there is one."""
    original = getattr(exc, "orig", None)
    for candidate in (original, getattr(original, "__cause__", None)):
        code = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if isinstance(code, str):
            return code
    return None


def constraint_name(exc: BaseException) -> str | None:
    """Name of the violated constraint, if the driver reported one."""
    original = getattr(exc, "orig", None)
    for candidate in (getattr(original, "__cause__", None), original):
        name = getattr(candidate, "constraint_name", None)
        if isinstance(name, str):
            return name
    return None


def is_unique_violation(exc: BaseException, constraint: str | None = None) -> bool:
    if not isinstance(exc, IntegrityError) or sqlstate(exc) != UNIQUE_VIOLATION:
        return False
    return constraint is None or constraint_name(exc) == constraint


def to_app_error(exc: SQLAlchemyError) -> AppError:
    """Fallback mapping for database errors a service did not handle itself."""
    code = sqlstate(exc)
    if isinstance(exc, IntegrityError):
        if code == UNIQUE_VIOLATION:
            return ConflictError("A record with the same unique value already exists.")
        if code == FOREIGN_KEY_VIOLATION:
            return ConflictError(
                "The record refers to something that does not exist, or is still in use.",
                code="reference_conflict",
            )
        if code in (NOT_NULL_VIOLATION, CHECK_VIOLATION):
            return ValidationFailedError("The data violates a database constraint.")
        return ConflictError()
    if code == INSUFFICIENT_PRIVILEGE:
        # Row-level security or a missing grant rejected the statement.
        return AppError(code="internal_error")
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return ServiceUnavailableError(
            "The database is temporarily unavailable.", code="database_unavailable"
        )
    return AppError(code="internal_error")
