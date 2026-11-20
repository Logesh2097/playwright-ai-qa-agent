"""Sensitive-action detection, metrics and HTTP hardening. No database."""

from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from app.core.metrics import Metrics
from app.middleware.http_security import SECURITY_HEADERS, HttpSecurityMiddleware
from app.testing.automation import Script
from app.testing.sensitive import kind_of, sensitive_steps

# --- sensitive actions --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Delete account", "delete"),
        ("Remove from cart", "delete"),
        ("Empty trash", "delete"),
        ("Pay now", "payment"),
        ("Place order", "payment"),
        ("Transfer funds", "payment"),
        ("Refund", "payment"),
        ("Send invitation", "message"),
        ("Email receipt", "message"),
        ("Publish post", "message"),
        ("/api/settings/notifications", "configuration"),
        ("Revoke API key", "configuration"),
        ("Cancel subscription", "configuration"),
        ("Deactivate user", "configuration"),
        ("Calculate points", None),
        ("Save", None),
        ("Sign in", None),
        ("Search", None),
        ("Next page", None),
        # Whole words only: these merely contain one.
        ("Display paytable", None),
        ("Resend", None),
        ("Undeleted items", None),
    ],
)
def test_what_counts_as_a_sensitive_action(text: str, kind: str | None) -> None:
    assert kind_of(text) == kind


def script(*steps: dict[str, Any], cleanup: list[dict[str, Any]] | None = None) -> Script:
    return Script.model_validate({"steps": list(steps), "cleanup": cleanup or []})


def button(name: str) -> dict[str, Any]:
    return {"action": "click", "locator": {"by": "role", "role": "button", "name": name}}


CHECK = {"action": "assert_title", "title": {"equals": "x"}}


def test_steps_that_commit_a_sensitive_action_are_found_with_where_and_why() -> None:
    found = sensitive_steps(
        script(
            {"action": "navigate", "url": "/account"},
            button("Delete account"),
            button("Pay now"),
            {"action": "api_request", "method": "DELETE", "url": "/api/orders/1", "save_as": "r"},
            {
                "action": "api_request",
                "method": "POST",
                "url": "/api/messages/send",
                "save_as": "m",
            },
            {
                "action": "select_option",
                "locator": {"by": "label", "value": "Plan"},
                "value": "Cancel subscription",
            },
            CHECK,
            cleanup=[button("Remove test order")],
        )
    )
    assert [(s.kind, s.where) for s in found] == [
        ("delete", "script step 2"),
        ("payment", "script step 3"),
        ("delete", "script step 4"),
        ("message", "script step 5"),
        ("configuration", "script step 6"),
        ("destructive_cleanup", "cleanup step 1"),
    ]
    assert found[0].reason == "script step 2 deletes records: Delete account"
    assert found[2].what == "DELETE /api/orders/1"


def test_reading_asserting_and_typing_are_not_sensitive() -> None:
    harmless = script(
        {"action": "navigate", "url": "/settings/delete-account"},  # opening a page changes nothing
        {
            "action": "assert_visible",
            "locator": {"by": "role", "role": "button", "name": "Delete account"},
        },
        {
            "action": "fill",
            "locator": {"by": "label", "value": "Email"},
            "value": "ada@example.com",
        },
        {
            "action": "fill",
            "locator": {"by": "label", "value": "Message"},
            "value": "Please delete everything",
        },
        {"action": "api_request", "method": "GET", "url": "/api/payments", "save_as": "r"},
        button("Calculate points"),
        button("Save"),
        CHECK,
    )
    assert sensitive_steps(harmless) == []
    assert sensitive_steps(None) == []


# --- metrics ------------------------------------------------------------------------------


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, float]] = {}

    def pipeline(self, transaction: bool = False) -> "FakeRedis":
        return self

    async def __aenter__(self) -> "FakeRedis":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def hincrbyfloat(self, key: str, name: str, value: float) -> None:
        bucket = self.hashes.setdefault(key, {})
        bucket[name] = bucket.get(name, 0) + value

    async def execute(self) -> None:
        return None

    async def hset(self, key: str, name: str, value: float) -> None:
        self.hashes.setdefault(key, {})[name] = value

    async def expire(self, key: str, seconds: int) -> None:
        return None

    async def hgetall(self, key: str) -> dict[bytes, bytes]:
        return {k.encode(): str(v).encode() for k, v in self.hashes.get(key, {}).items()}


async def test_metrics_are_counted_together_and_rendered_as_prometheus_text() -> None:
    api, worker, redis = Metrics(), Metrics(), FakeRedis()
    api.bind(redis, process="api")
    worker.bind(redis, process="worker")

    await api.inc("qa_http_requests_total", {"method": "GET", "route": "/x/{id}", "status": 200})
    await api.inc("qa_http_requests_total", {"method": "GET", "route": "/x/{id}", "status": 200})
    await worker.inc("qa_executions_total", {"status": "completed", "verdict": "failed"})
    await worker.observe("qa_execution_duration_seconds", 12.5)
    await worker.observe("qa_execution_duration_seconds", 7.5)
    await worker.gauge("qa_service_up", 1, {"service": "engine"})
    await api.inc(
        "qa_llm_calls_total", {"model": 'odd"name\nx', "purpose": "analysis", "status": "ok"}
    )

    text = await api.render()

    assert (
        '# TYPE qa_http_requests_total counter\nqa_http_requests_total{method="GET",route="/x/{id}",status="200"} 2'
        in text
    )
    assert 'qa_executions_total{status="completed",verdict="failed"} 1' in text
    assert (
        "qa_execution_duration_seconds_sum 20" in text
        and "qa_execution_duration_seconds_count 2" in text
    )
    assert '# TYPE qa_service_up gauge\nqa_service_up{service="engine"} 1' in text
    assert 'qa_process_resident_memory_bytes{process="api"}' in text
    # A label value cannot break out of its quotes or its line.
    assert 'model="odd\\"name x"' in text
    for line in text.splitlines():
        assert (
            line.startswith("#")
            or line.rsplit(" ", 1)[1].replace(".", "").replace("e+", "").replace("-", "").isdigit()
        ), line


async def test_metrics_never_raise_without_redis_or_with_a_broken_one() -> None:
    unbound = Metrics()
    await unbound.inc("qa_x_total")
    await unbound.observe("qa_x_seconds", 1.0)
    assert await unbound.render() == ""

    class Broken:
        def pipeline(self, **_: Any) -> Any:
            raise ConnectionError("redis is down")

        async def hgetall(self, key: str) -> Any:
            raise ConnectionError("redis is down")

        async def hset(self, *_: Any) -> Any:
            raise ConnectionError("redis is down")

    broken = Metrics()
    broken.bind(Broken(), process="api")
    await broken.inc("qa_x_total")
    assert await broken.render() == ""


# --- HTTP hardening -----------------------------------------------------------------------


def hardened(max_body: int = 100, hsts: bool = False) -> httpx.AsyncClient:
    async def echo(request: Request) -> Response:
        return JSONResponse({"bytes": len(await request.body())})

    async def file(request: Request) -> Response:
        return Response(
            b"x",
            headers={
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "Cache-Control": "private, no-store",
            },
        )

    async def broken(request: Request) -> Response:
        raise RuntimeError("boom")

    app = Starlette(
        routes=[
            Route("/echo", echo, methods=["POST", "GET"]),
            Route("/file", file),
            Route("/broken", broken),
        ],
        # Inside the application's own error handling, as in the real service.
        middleware=[Middleware(HttpSecurityMiddleware, max_body_bytes=max_body, hsts=hsts)],
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://t"
    )


async def test_every_answer_carries_the_protective_headers() -> None:
    async with hardened() as client:
        ok = await client.get("/echo")
        missing = await client.get("/nothing-here")
        file = await client.get("/file")
    for response in (ok, missing):
        for name, value in SECURITY_HEADERS.items():
            assert response.headers[name] == value, (response.status_code, name)
        assert response.headers["cache-control"] == "no-store"
        assert "strict-transport-security" not in response.headers
    # An endpoint's own, stricter policy is kept.
    assert file.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert (
        file.headers["cache-control"] == "private, no-store"
        and file.headers["x-frame-options"] == "DENY"
    )
    async with hardened(hsts=True) as secure:
        assert (
            "max-age=31536000" in (await secure.get("/echo")).headers["strict-transport-security"]
        )


async def test_a_body_over_the_limit_is_refused_by_its_declared_or_its_actual_size() -> None:
    async def chunks() -> Any:
        for _ in range(30):
            yield b"x" * 10

    async with hardened(max_body=100) as client:
        fits = await client.post("/echo", content=b"x" * 100)
        declared = await client.post("/echo", content=b"x" * 101)
        streamed = await client.post("/echo", content=chunks())  # no Content-Length

    assert (fits.status_code, fits.json()) == (200, {"bytes": 100})
    for refused in (declared, streamed):
        assert refused.status_code == 413 and refused.json()["code"] == "payload_too_large"
        assert refused.headers["x-content-type-options"] == "nosniff"


# --- outgoing mail ------------------------------------------------------------------------


async def test_mail_goes_out_encrypted_and_a_failed_delivery_never_logs_the_link(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import smtplib

    from app.core.config import Settings
    from app.services.email import (
        EmailMessage,
        LogEmailSender,
        SmtpEmailSender,
        build_email_sender,
    )

    calls: list[Any] = []

    class FakeSmtp:
        def __init__(self, host: str, port: int, timeout: int) -> None:
            calls.append(("connect", host, port))

        def __enter__(self) -> "FakeSmtp":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def starttls(self, context: Any) -> None:
            calls.append(("starttls",))

        def login(self, username: str, password: str) -> None:
            calls.append(("login", username))

        def send_message(self, message: Any) -> None:
            calls.append(
                (
                    "send",
                    message["From"],
                    message["To"],
                    message["Subject"],
                    message.get_content().strip(),
                )
            )

    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    base: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://u:p@localhost/db", "redis_url": "redis://localhost:6379/0",
        "jwt_secret_key": "k" * 40, "credential_master_keys": "1:" + "A" * 43 + "=",
    }  # fmt: skip
    settings = Settings(
        **base,
        smtp_host="mail.acme.io",
        smtp_username="mailer",
        smtp_password="s3cret-pass",
        smtp_from="qa@acme.io",
    )
    sender = build_email_sender(settings)
    assert isinstance(sender, SmtpEmailSender)
    link = "https://qa.acme.io/reset-password?token=single-use-token-value"

    await sender.send(EmailMessage(to="ada@acme.io", subject="Reset your password", body=link))

    assert calls == [
        ("connect", "mail.acme.io", 587),
        ("starttls",),  # before anything is sent
        ("login", "mailer"),
        ("send", "qa@acme.io", "ada@acme.io", "Reset your password", link),
    ]

    def refuse(self: Any, message: Any) -> None:
        raise smtplib.SMTPRecipientsRefused({})

    monkeypatch.setattr(FakeSmtp, "send_message", refuse)
    with caplog.at_level("INFO"):
        await sender.send(EmailMessage(to="ada@acme.io", subject="Reset your password", body=link))
    logged = " ".join(str(record.__dict__) for record in caplog.records)
    assert (
        "email_not_delivered" in logged
        and "single-use-token-value" not in logged
        and "s3cret-pass" not in logged
    )
    # Without a mail server, production never logs the body either.
    assert isinstance(build_email_sender(Settings(**base)), LogEmailSender)
