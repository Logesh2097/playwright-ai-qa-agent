"""Builds the single error envelope used by every error response."""

from collections.abc import Mapping, Sequence

from fastapi.responses import JSONResponse

from app.core.errors import ErrorDetail
from app.core.logging import get_request_id
from app.schemas.common import ErrorItem, ProblemDetail

PROBLEM_MEDIA_TYPE = "application/problem+json"
_TYPE_BASE = "about:blank#"


def problem_response(
    *,
    status: int,
    code: str,
    title: str,
    detail: str | None = None,
    errors: Sequence[ErrorDetail] = (),
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body = ProblemDetail(
        type=f"{_TYPE_BASE}{code}",
        title=title,
        status=status,
        code=code,
        detail=detail,
        errors=[ErrorItem(field=e.field, code=e.code, message=e.message) for e in errors],
        request_id=get_request_id(),
    )
    return JSONResponse(
        status_code=status,
        content=body.model_dump(mode="json"),
        media_type=PROBLEM_MEDIA_TYPE,
        headers=dict(headers) if headers else None,
    )
