"""What every step handler is given, and helpers they share."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator as Element
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.engine.browser import Scope
from app.engine.errors import StepError
from app.engine.evidence import Evidence
from app.engine.locators import find, matched, record
from app.engine.protocol import ArtifactRef, Timeouts
from app.engine.values import Values
from app.testing.automation import Locator

POLL_INTERVAL_S = 0.1


@dataclass(slots=True)
class StepContext:
    page: Page
    context: BrowserContext
    values: Values
    scope: Scope
    evidence: Evidence
    timeouts: Timeouts
    allowed_actions: frozenset[str]
    files: dict[str, Path]


@dataclass(slots=True)
class StepOutcome:
    """What a step that passed has to report."""

    actual: dict[str, Any] | None = None
    expected: dict[str, Any] | None = None
    locator: dict[str, Any] | None = None
    artifact: ArtifactRef | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def action_timeout(step: Any, ctx: StepContext) -> int:
    return int(getattr(step, "timeout_ms", None) or ctx.timeouts.action_ms)


def first_line(error: BaseException) -> str:
    text = str(error).strip().splitlines()
    return text[0][:400] if text else type(error).__name__


async def on_element(
    ctx: StepContext,
    locator: Locator,
    operation: Callable[[Element], Awaitable[Any]],
) -> tuple[Any, dict[str, Any]]:
    """Perform an operation on the one element a locator describes.

    Playwright waits for the element to be ready. If that does not happen, the
    reason is worked out here: nothing matched, several matched, or the element
    was there but not ready.
    """
    element = find(ctx.page, locator, ctx.values)
    what = locator.describe()
    try:
        result = await operation(element)
    except PlaywrightTimeoutError:
        count = await matched(element)
        if count == 0:
            raise StepError("locator_not_found", f"No element matches {what}.") from None
        if count is not None and count > 1:
            raise StepError(
                "locator_ambiguous", f"{count} elements match {what}; a locator must match one."
            ) from None
        raise StepError(
            "timeout", f"{what} was found, but was not ready for the action in time."
        ) from None
    except PlaywrightError as exc:
        message = first_line(exc)
        if "strict mode violation" in message:
            count = await matched(element)
            raise StepError(
                "locator_ambiguous", f"{count} elements match {what}; a locator must match one."
            ) from None
        raise StepError("action_failed", message) from None
    return result, record(locator, ctx.values, 1)


async def examine(
    ctx: StepContext,
    locator: Locator,
    read: Callable[[Element], Awaitable[Any]],
    accept: Callable[[Any], bool],
    timeout_ms: int,
) -> tuple[bool, Any, dict[str, Any]]:
    """Read something from one element until it is acceptable or time runs out.

    Returns (accepted, last value read, locator record). Raises if the locator
    never settled on exactly one element: then there was nothing to examine.
    """
    element = find(ctx.page, locator, ctx.values)
    deadline = time.monotonic() + timeout_ms / 1000
    count: int | None = None
    value: Any = None
    while True:
        count = await matched(element)
        if count == 1:
            try:
                value = await read(element)
            except PlaywrightError:
                count = None  # changed under us; look again
            else:
                if accept(value):
                    return True, value, record(locator, ctx.values, 1)
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(POLL_INTERVAL_S)
    what = locator.describe()
    if count == 0:
        raise StepError("locator_not_found", f"No element matches {what}.")
    if count is not None and count > 1:
        raise StepError(
            "locator_ambiguous", f"{count} elements match {what}; a locator must match one."
        )
    return False, value, record(locator, ctx.values, count)


async def poll(
    read: Callable[[], Awaitable[Any]], accept: Callable[[Any], bool], timeout_ms: int
) -> tuple[bool, Any]:
    """Read a value until it is acceptable or time runs out."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        try:
            value = await read()
        except PlaywrightError:
            value = None
        else:
            if accept(value):
                return True, value
        if time.monotonic() >= deadline:
            return False, value
        await asyncio.sleep(POLL_INTERVAL_S)
