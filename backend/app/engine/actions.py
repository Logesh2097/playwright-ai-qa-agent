"""The steps that do something: navigate, click, type, choose, upload, wait, read, call an API."""

import json
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.browser.redaction import sanitize_url
from app.engine.errors import StepError
from app.engine.matching import matches, url_subject
from app.engine.steps import StepContext, StepOutcome, action_timeout, first_line, on_element
from app.testing import automation as dsl
from app.testing.expressions import ExpressionError, to_number

_MAX_BODY_CHARS = 200_000


async def navigate(step: dsl.Navigate, ctx: StepContext) -> StepOutcome:
    url = ctx.scope.absolute(ctx.values.render(step.url))
    ctx.scope.check(url)
    timeout = step.timeout_ms or ctx.timeouts.navigation_ms
    try:
        response = await ctx.page.goto(url, timeout=timeout)
    except PlaywrightTimeoutError:
        raise StepError("timeout", f"{sanitize_url(url)} did not load in time.") from None
    except PlaywrightError as exc:
        message = first_line(exc)
        blocked = any(
            marker in message
            for marker in (
                "ERR_BLOCKED_BY_CLIENT",
                "ERR_TUNNEL_CONNECTION_FAILED",
                "NS_ERROR_PROXY",
            )
        )
        raise StepError("destination_not_allowed" if blocked else "navigation", message) from None
    # A redirect may have led elsewhere.
    ctx.scope.check(ctx.page.url)
    return StepOutcome(
        actual={"url": ctx.page.url, "status": response.status if response else None}
    )


async def click(step: dsl.Click, ctx: StepContext) -> StepOutcome:
    timeout = action_timeout(step, ctx)
    _, locator = await on_element(ctx, step.locator, lambda e: e.click(timeout=timeout))
    return StepOutcome(locator=locator)


async def fill(step: dsl.Fill, ctx: StepContext) -> StepOutcome:
    value = ctx.values.render(step.value)
    timeout = action_timeout(step, ctx)
    secret = ctx.values.uses_secret(step)

    async def operation(element: Any) -> None:
        await element.fill(value, timeout=timeout)
        if secret:
            ctx.evidence.masked.append(element)

    _, locator = await on_element(ctx, step.locator, operation)
    return StepOutcome(locator=locator, actual={"value": value})


async def clear(step: dsl.Clear, ctx: StepContext) -> StepOutcome:
    timeout = action_timeout(step, ctx)
    _, locator = await on_element(ctx, step.locator, lambda e: e.clear(timeout=timeout))
    return StepOutcome(locator=locator)


async def select_option(step: dsl.SelectOption, ctx: StepContext) -> StepOutcome:
    timeout = action_timeout(step, ctx)
    choice: dict[str, Any] = (
        {"value": ctx.values.render(step.value)}
        if step.value is not None
        else {"label": ctx.values.render(step.label or "")}
    )
    selected, locator = await on_element(
        ctx, step.locator, lambda e: e.select_option(timeout=timeout, **choice)
    )
    return StepOutcome(locator=locator, actual={"selected": selected})


async def check(step: dsl.Check, ctx: StepContext) -> StepOutcome:
    timeout = action_timeout(step, ctx)
    _, locator = await on_element(ctx, step.locator, lambda e: e.check(timeout=timeout))
    return StepOutcome(locator=locator)


async def uncheck(step: dsl.Uncheck, ctx: StepContext) -> StepOutcome:
    timeout = action_timeout(step, ctx)
    _, locator = await on_element(ctx, step.locator, lambda e: e.uncheck(timeout=timeout))
    return StepOutcome(locator=locator)


async def select_radio(step: dsl.SelectRadio, ctx: StepContext) -> StepOutcome:
    timeout = action_timeout(step, ctx)

    async def operation(element: Any) -> None:
        kind = await element.get_attribute("type", timeout=timeout)
        role = await element.get_attribute("role", timeout=timeout)
        if kind != "radio" and role != "radio":
            raise StepError("action_failed", f"{step.locator.describe()} is not a radio button.")
        await element.check(timeout=timeout)

    _, locator = await on_element(ctx, step.locator, operation)
    return StepOutcome(locator=locator)


async def upload_file(step: dsl.UploadFile, ctx: StepContext) -> StepOutcome:
    path = ctx.files.get(step.file)
    if path is None:
        raise StepError("test_data", f"There is no approved test file named '{step.file}'.")
    timeout = action_timeout(step, ctx)
    _, locator = await on_element(
        ctx, step.locator, lambda e: e.set_input_files(str(path), timeout=timeout)
    )
    return StepOutcome(locator=locator, actual={"file": step.file})


async def wait_for_element(step: dsl.WaitForElement, ctx: StepContext) -> StepOutcome:
    timeout = action_timeout(step, ctx)
    _, locator = await on_element(
        ctx, step.locator, lambda e: e.wait_for(state=step.state, timeout=timeout)
    )
    locator["matched"] = None if step.state in ("hidden", "detached") else 1
    return StepOutcome(locator=locator, expected={"state": step.state})


async def wait_for_navigation(step: dsl.WaitForNavigation, ctx: StepContext) -> StepOutcome:
    timeout = step.timeout_ms or ctx.timeouts.navigation_ms
    expected = ctx.values.render(step.url.text)
    try:
        await ctx.page.wait_for_url(
            lambda url: matches(step.url, expected, url_subject(url, expected)), timeout=timeout
        )
    except PlaywrightTimeoutError:
        raise StepError(
            "timeout",
            f"The browser did not arrive at an address that {step.url.kind} {expected!r}; "
            f"it is at {sanitize_url(ctx.page.url)}.",
        ) from None
    ctx.scope.check(ctx.page.url)
    return StepOutcome(actual={"url": ctx.page.url}, expected={step.url.kind: expected})


async def read_text(step: dsl.ReadText, ctx: StepContext) -> StepOutcome:
    timeout = action_timeout(step, ctx)
    text, locator = await on_element(ctx, step.locator, lambda e: e.inner_text(timeout=timeout))
    value: Any = " ".join(str(text).split())
    if step.as_number:
        try:
            value = to_number(value)
        except ExpressionError as exc:
            raise StepError("action_failed", f"{step.locator.describe()}: {exc}") from None
    ctx.values.saved[step.save_as] = value
    return StepOutcome(locator=locator, actual={"value": str(value), "saved_as": step.save_as})


async def read_attribute(step: dsl.ReadAttribute, ctx: StepContext) -> StepOutcome:
    timeout = action_timeout(step, ctx)
    value, locator = await on_element(
        ctx, step.locator, lambda e: e.get_attribute(step.attribute, timeout=timeout)
    )
    if value is None:
        raise StepError(
            "action_failed", f"{step.locator.describe()} has no attribute '{step.attribute}'."
        )
    ctx.values.saved[step.save_as] = value
    return StepOutcome(locator=locator, actual={"value": value, "saved_as": step.save_as})


async def screenshot(step: dsl.Screenshot, ctx: StepContext) -> StepOutcome:
    artifact = await ctx.evidence.screenshot(
        ctx.page, step.name or "step", full_page=step.full_page
    )
    if artifact is None:
        raise StepError("action_failed", "The screenshot could not be taken.")
    return StepOutcome(artifact=artifact)


async def api_request(step: dsl.ApiRequest, ctx: StepContext) -> StepOutcome:
    """An HTTP request sharing the browser context's cookies, so it acts as the signed-in user."""
    url = ctx.scope.absolute(ctx.values.render(step.url))
    ctx.scope.check(url)
    options: dict[str, Any] = {
        "method": step.method,
        "headers": {name: ctx.values.render(value) for name, value in step.headers.items()},
        "timeout": step.timeout_ms or ctx.timeouts.navigation_ms,
        # A redirect is an answer in its own right; it is returned, not followed.
        "max_redirects": 0,
        "fail_on_status_code": False,
    }
    if step.json_body is not None:
        options["data"] = json.dumps(ctx.values.render_any(step.json_body))
        options["headers"].setdefault("Content-Type", "application/json")
    try:
        response = await ctx.context.request.fetch(url, **options)
        text = (await response.text())[:_MAX_BODY_CHARS]
    except PlaywrightTimeoutError:
        raise StepError("timeout", f"{sanitize_url(url)} did not answer in time.") from None
    except PlaywrightError as exc:
        raise StepError("request_failed", first_line(exc)) from None
    refusal = response.headers.get("x-egress-reason")
    if refusal is not None:
        # The egress proxy answered, not the application: the request never arrived.
        raise StepError(
            "destination_not_allowed",
            f"{sanitize_url(url)} was refused by the egress proxy ({refusal[:60]}).",
        )
    try:
        body: Any = json.loads(text)
        is_json = True
    except ValueError:
        body, is_json = None, False
    ctx.values.responses[step.save_as] = {
        "status": response.status,
        "headers": dict(response.headers),
        "json": body,
        "is_json": is_json,
        "text": text,
    }
    return StepOutcome(
        actual={
            "status": response.status,
            "url": urlsplit(url).path or "/",
            "saved_as": step.save_as,
            "is_json": is_json,
        }
    )


Handler = Callable[[Any, StepContext], Awaitable[StepOutcome]]
ACTIONS: dict[str, Handler] = {
    "navigate": navigate,
    "click": click,
    "fill": fill,
    "clear": clear,
    "select_option": select_option,
    "check": check,
    "uncheck": uncheck,
    "select_radio": select_radio,
    "upload_file": upload_file,
    "wait_for_element": wait_for_element,
    "wait_for_navigation": wait_for_navigation,
    "read_text": read_text,
    "read_attribute": read_attribute,
    "screenshot": screenshot,
    "api_request": api_request,
}
