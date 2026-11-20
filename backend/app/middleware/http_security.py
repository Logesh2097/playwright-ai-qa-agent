"""Response headers, request size limits and request metrics. Pure ASGI.

Every answer of the API carries the same protective headers, whatever produced
it. A request body larger than the limit is refused before the application
reads it, by its declared length or, without one, as soon as the bytes received
pass the limit.
"""

import time

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.metrics import metrics
from app.core.problem import problem_response

# The API returns data, never pages: nothing may be loaded, framed or embedded.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Resource-Policy": "same-site",
}
HSTS = "max-age=31536000; includeSubDomains"
_QUIET = frozenset({"/healthz", "/readyz", "/metrics"})


class BodyTooLargeError(Exception):
    pass


class HttpSecurityMiddleware:
    def __init__(self, app: ASGIApp, *, max_body_bytes: int, hsts: bool) -> None:
        self.app = app
        self._max = max_body_bytes
        self._hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500
        headers_sent = False

        async def guarded_send(message: Message) -> None:
            nonlocal status, headers_sent
            if message["type"] == "http.response.start":
                headers_sent = True
                status = message["status"]
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    # An endpoint that serves a file sets a stricter policy of its own.
                    if name not in headers:
                        headers[name] = value
                if self._hsts:
                    headers["Strict-Transport-Security"] = HSTS
                if "cache-control" not in headers:
                    headers["Cache-Control"] = "no-store"
            await send(message)

        async def refuse() -> None:
            await metrics.inc("qa_rejected_requests_total", {"reason": "body_too_large"})
            response = problem_response(
                status=413,
                code="payload_too_large",
                title="Payload too large",
                detail=f"The request body is larger than {self._max} bytes.",
            )
            await response(scope, receive, guarded_send)

        declared = Headers(scope=scope).get("content-length", "")
        try:
            if declared.isdigit() and int(declared) > self._max:
                await refuse()
                return
            received = 0

            async def limited_receive() -> Message:
                nonlocal received
                message = await receive()
                if message["type"] == "http.request":
                    received += len(message.get("body", b""))
                    if received > self._max:
                        raise BodyTooLargeError
                return message

            try:
                await self.app(scope, limited_receive, guarded_send)
            except BodyTooLargeError:
                if headers_sent:
                    raise
                await refuse()
        finally:
            path = scope["path"]
            if path not in _QUIET:
                route = scope.get("route")
                # The route's template, so that identifiers never become label values.
                template = getattr(route, "path", None) or "unmatched"
                labels = {"method": scope["method"], "route": template}
                await metrics.add(
                    {
                        _label("qa_http_requests_total", {**labels, "status": status}): 1,
                        _label("qa_http_request_duration_seconds_sum", labels): time.perf_counter()
                        - started,
                        _label("qa_http_request_duration_seconds_count", labels): 1,
                        **(
                            {
                                _label(
                                    "qa_http_errors_total", {"route": template, "status": status}
                                ): 1
                            }
                            if status >= 500
                            else {}
                        ),
                        **(
                            {_label("qa_rejected_requests_total", {"reason": "rate_limited"}): 1}
                            if status == 429
                            else {}
                        ),
                    }
                )


def _label(metric: str, labels: dict[str, object]) -> str:
    from app.core.metrics import _name

    return _name(metric, labels)
