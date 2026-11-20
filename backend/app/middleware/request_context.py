"""Request ID propagation, access logging, and the last-resort error response."""

import logging
import re
import time

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import request_id_var
from app.core.problem import problem_response
from app.utils.ids import new_request_id

logger = logging.getLogger("app.access")

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_QUIET_PATHS = frozenset({"/healthz", "/readyz"})


class RequestContextMiddleware:
    """Pure ASGI middleware.

    - Accepts a well-formed inbound ``X-Request-ID`` or generates one.
    - Exposes it to logging (context variable), handlers (``request.state``)
      and the client (response header).
    - Logs one access line per request.
    - Converts any exception that escaped the registered handlers into the
      standard 500 error envelope, so internals are never returned to clients.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        inbound = Headers(scope=scope).get(REQUEST_ID_HEADER, "")
        request_id = inbound if _VALID_REQUEST_ID.fullmatch(inbound) else new_request_id()
        token = request_id_var.set(request_id)
        scope.setdefault("state", {})["request_id"] = request_id

        started = time.perf_counter()
        status_code = 500
        response_started = False

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            logger.exception(
                "unhandled_exception", extra={"method": scope["method"], "path": scope["path"]}
            )
            if response_started:
                raise
            response = problem_response(
                status=500, code="internal_error", title="Internal server error"
            )
            await response(scope, receive, send_with_request_id)
        finally:
            level = logging.DEBUG if scope["path"] in _QUIET_PATHS else logging.INFO
            logger.log(
                level,
                "request",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            request_id_var.reset(token)
