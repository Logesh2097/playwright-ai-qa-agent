"""Browser management: one browser per run, one fresh context per case attempt.

A context is the unit of isolation between cases: its own cookies, storage and
cache. Nothing carries over from one case to the next unless the case signs in.
"""

import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Playwright,
    Request,
    Route,
    async_playwright,
)

from app.browser.redaction import sanitize_url
from app.core.security.url_safety import Origin, UrlNotAllowedError, parse_origin
from app.engine.errors import StepError
from app.engine.protocol import RunPlan

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_MAX_LOG = 50
_MAX_CONSOLE = 300


class Scope:
    """The addresses a run may open. The egress proxy enforces this as well; here it
    gives an immediate, explainable refusal and a record of what was attempted."""

    def __init__(self, plan: RunPlan) -> None:
        self._origins = frozenset(parse_origin(o, allow_http=True) for o in plan.allowed_origins)
        self._allow_http = plan.allow_http
        base = urlsplit(plan.base_url)
        self._base = f"{base.scheme}://{base.netloc}"
        self.read_only = plan.read_only

    def absolute(self, url: str) -> str:
        """A path is relative to the environment's address."""
        return self._base + url if url.startswith("/") and not url.startswith("//") else url

    def origin_of(self, url: str) -> Origin | None:
        try:
            return parse_origin(url, allow_http=self._allow_http)
        except UrlNotAllowedError:
            return None

    def allows(self, url: str) -> bool:
        return self.origin_of(url) in self._origins

    def check(self, url: str) -> None:
        if not self.allows(url):
            raise StepError(
                "destination_not_allowed",
                f"{sanitize_url(url)} is outside this environment's allowed origins.",
            )


@dataclass(slots=True)
class PageLog:
    """What the browser reported while a case ran. Evidence, not a verdict."""

    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    failed_requests: list[dict[str, Any]] = field(default_factory=list)
    blocked_requests: list[dict[str, Any]] = field(default_factory=list)
    # Every console message in order, errors included, for the console log evidence.
    console: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "console_log": self.console,
            "console_errors": self.console_errors,
            "page_errors": self.page_errors,
            "failed_requests": self.failed_requests,
            "blocked_requests": self.blocked_requests,
        }


def _console(log: PageLog, kind: str, text: str) -> None:
    if len(log.console) < _MAX_CONSOLE:
        log.console.append({"at": datetime.now(UTC).isoformat(), "type": kind, "text": text[:500]})
    if kind == "error":
        _note(log.console_errors, text[:500])


def _note(items: list[Any], item: Any) -> None:
    if len(items) < _MAX_LOG:
        items.append(item)


class BrowserManager:
    def __init__(self, plan: RunPlan) -> None:
        self._plan = plan
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self.scope = Scope(plan)

    async def __aenter__(self) -> Self:
        plan = self._plan
        self._playwright = await async_playwright().start()
        options: dict[str, Any] = {"headless": True}
        if plan.proxy is not None:
            options["proxy"] = {
                "server": plan.proxy.server,
                "username": plan.proxy.username,
                "password": plan.proxy.password,
                # Loopback addresses must go through the proxy too, where they are refused.
                "bypass": "<-loopback>",
            }
        name = plan.config.browser
        if name == "firefox":
            options["firefox_user_prefs"] = {"network.proxy.allow_hijacking_localhost": True}
        self._browser = await getattr(self._playwright, name).launch(**options)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        with contextlib.suppress(Exception):
            if self._browser is not None:
                await self._browser.close()
        with contextlib.suppress(Exception):
            if self._playwright is not None:
                await self._playwright.stop()

    @property
    def version(self) -> str:
        assert self._browser is not None  # noqa: S101
        return self._browser.version

    async def new_context(
        self, *, storage_state: Any = None, video_dir: str | None = None
    ) -> tuple[BrowserContext, PageLog]:
        assert self._browser is not None  # noqa: S101
        config = self._plan.config
        size = {"width": config.viewport.width, "height": config.viewport.height}
        options: dict[str, Any] = {
            "viewport": size,
            "accept_downloads": False,
            "service_workers": "block",
            "storage_state": storage_state,
        }
        if video_dir is not None:
            options["record_video_dir"] = video_dir
            options["record_video_size"] = size
        context = await self._browser.new_context(**options)
        context.set_default_timeout(config.timeouts.action_ms)
        context.set_default_navigation_timeout(config.timeouts.navigation_ms)
        log = PageLog()
        await context.route("**/*", lambda route, request: self._guard(route, request, log))
        context.on("console", lambda message: _console(log, message.type, message.text))
        context.on("weberror", lambda error: _note(log.page_errors, str(error.error)[:500]))
        context.on(
            "requestfailed",
            lambda request: _note(
                log.failed_requests,
                {
                    "url": sanitize_url(request.url),
                    "method": request.method,
                    "error": request.failure,
                },
            ),
        )
        context.on(
            "response",
            lambda response: (
                _note(
                    log.failed_requests,
                    {
                        "url": sanitize_url(response.url),
                        "method": response.request.method,
                        "status": response.status,
                    },
                )
                if response.status >= 500
                else None
            ),
        )
        return context, log

    async def _guard(self, route: Route, request: Request, log: PageLog) -> None:
        """Abort requests to other origins, and writes on read-only environments."""
        url = request.url
        if urlsplit(url).scheme not in ("http", "https"):
            await route.continue_()  # data:, blob:, about:
            return
        reason: str | None = None
        if not self.scope.allows(url):
            reason = "origin_not_allowlisted"
        elif self.scope.read_only and request.method not in _READ_METHODS:
            reason = "environment_read_only"
        if reason is None:
            await route.continue_()
            return
        _note(
            log.blocked_requests,
            {"url": sanitize_url(url), "method": request.method, "reason": reason},
        )
        await route.abort("blockedbyclient")
