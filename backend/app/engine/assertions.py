"""The steps that check something. Each either holds, fails or cannot be evaluated.

An assertion waits: it re-reads the page until what it expects is there or its
time is up, so a page that is still rendering does not produce a false failure.
There are no fixed sleeps.

- Holds: returns what was observed.
- Does not hold: `StepFailure`, with what was expected and what was found.
- Cannot be evaluated (no such element, several, no saved response):
  `StepError`. That says nothing about the application, so it is not a failure.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from playwright.async_api import Locator as Element

from app.engine.errors import StepError, StepFailure
from app.engine.locators import find, matched, record
from app.engine.matching import json_pointer, json_type, matches, squash, url_subject
from app.engine.steps import StepContext, StepOutcome, action_timeout, examine, poll
from app.testing import automation as dsl
from app.testing.expressions import ExpressionError, evaluate, names_used


def _fail(what: str, expected: dict[str, Any], actual: dict[str, Any]) -> StepFailure:
    return StepFailure(
        f"{what}: expected {expected}, found {actual}.", expected=expected, actual=actual
    )


# --- presence and visibility: "no such element" is an answer here -------------------------


async def _count_until(
    step: Any, ctx: StepContext, accept: Callable[[int], bool]
) -> tuple[bool, int, Element]:
    element = find(ctx.page, step.locator, ctx.values)

    async def read() -> int:
        return await element.count()

    ok, count = await poll(read, lambda c: c is not None and accept(c), action_timeout(step, ctx))
    return ok, int(count or 0), element


async def assert_exists(step: dsl.AssertExists, ctx: StepContext) -> StepOutcome:
    ok, count, _ = await _count_until(step, ctx, lambda c: c == 1)
    locator = record(step.locator, ctx.values, count)
    if count > 1:
        raise StepError(
            "locator_ambiguous",
            f"{count} elements match {step.locator.describe()}; use assert_count for several.",
        )
    if not ok:
        raise _fail(step.locator.describe(), {"exists": True}, {"exists": False})
    return StepOutcome(locator=locator, expected={"exists": True}, actual={"exists": True})


async def assert_absent(step: dsl.AssertAbsent, ctx: StepContext) -> StepOutcome:
    ok, count, _ = await _count_until(step, ctx, lambda c: c == 0)
    if not ok:
        raise _fail(step.locator.describe(), {"exists": False}, {"exists": True, "matched": count})
    return StepOutcome(
        locator=record(step.locator, ctx.values, 0),
        expected={"exists": False},
        actual={"exists": False},
    )


async def assert_count(step: dsl.AssertCount, ctx: StepContext) -> StepOutcome:
    ok, count, _ = await _count_until(step, ctx, lambda c: c == step.count)
    if not ok:
        raise _fail(step.locator.describe(), {"count": step.count}, {"count": count})
    return StepOutcome(
        locator=record(step.locator, ctx.values, count),
        expected={"count": step.count},
        actual={"count": count},
    )


async def _visibility(step: Any, ctx: StepContext, *, want: bool) -> StepOutcome:
    element = find(ctx.page, step.locator, ctx.values)

    async def read() -> tuple[int, bool]:
        count = await element.count()
        return count, (await element.is_visible()) if count == 1 else False

    def accept(state: tuple[int, bool] | None) -> bool:
        if state is None:
            return False
        count, visible = state
        return (count == 1 and visible) if want else (count == 0 or (count == 1 and not visible))

    ok, state = await poll(read, accept, action_timeout(step, ctx))
    count, visible = state if state is not None else (await matched(element) or 0, False)
    if count > 1:
        raise StepError(
            "locator_ambiguous",
            f"{count} elements match {step.locator.describe()}; a locator must match one.",
        )
    expected, actual = {"visible": want}, {"visible": visible, "matched": count}
    if not ok:
        raise _fail(step.locator.describe(), expected, actual)
    return StepOutcome(
        locator=record(step.locator, ctx.values, count), expected=expected, actual=actual
    )


async def assert_visible(step: dsl.AssertVisible, ctx: StepContext) -> StepOutcome:
    return await _visibility(step, ctx, want=True)


async def assert_hidden(step: dsl.AssertHidden, ctx: StepContext) -> StepOutcome:
    return await _visibility(step, ctx, want=False)


# --- properties of one element: it has to exist to be examined ----------------------------


async def _property(
    step: Any,
    ctx: StepContext,
    read: Callable[[Element], Awaitable[Any]],
    accept: Callable[[Any], bool],
    expected: dict[str, Any],
    name: str,
) -> StepOutcome:
    ok, value, locator = await examine(ctx, step.locator, read, accept, action_timeout(step, ctx))
    actual = {name: value}
    if not ok:
        raise _fail(step.locator.describe(), expected, actual)
    return StepOutcome(locator=locator, expected=expected, actual=actual)


async def assert_enabled(step: dsl.AssertEnabled, ctx: StepContext) -> StepOutcome:
    return await _property(
        step, ctx, lambda e: e.is_enabled(), lambda v: v is True, {"enabled": True}, "enabled"
    )


async def assert_disabled(step: dsl.AssertDisabled, ctx: StepContext) -> StepOutcome:
    return await _property(
        step, ctx, lambda e: e.is_enabled(), lambda v: v is False, {"enabled": False}, "enabled"
    )


async def assert_checked(step: dsl.AssertChecked, ctx: StepContext) -> StepOutcome:
    return await _property(
        step,
        ctx,
        lambda e: e.is_checked(),
        lambda v: v is step.checked,
        {"checked": step.checked},
        "checked",
    )


async def assert_text(step: dsl.AssertText, ctx: StepContext) -> StepOutcome:
    expected = ctx.values.render(step.text.text)

    async def read(element: Element) -> str:
        return squash(await element.inner_text(timeout=1_000))

    return await _property(
        step,
        ctx,
        read,
        lambda v: matches(step.text, expected, v),
        {step.text.kind: expected},
        "text",
    )


async def assert_value(step: dsl.AssertValue, ctx: StepContext) -> StepOutcome:
    expected = ctx.values.render(step.value.text)
    return await _property(
        step,
        ctx,
        lambda e: e.input_value(timeout=1_000),
        lambda v: matches(step.value, expected, v),
        {step.value.kind: expected},
        "value",
    )


async def assert_validation_message(
    step: dsl.AssertValidationMessage, ctx: StepContext
) -> StepOutcome:
    """The message the browser itself would show for an invalid field."""
    message = step.message
    expected_text = ctx.values.render(message.text) if message is not None else None

    async def read(element: Element) -> str:
        # A fixed script: nothing from the test case is evaluated in the page.
        return str(await element.evaluate("element => element.validationMessage || ''"))

    def accept(value: Any) -> bool:
        if message is None or expected_text is None:
            return bool(value)
        return bool(value) and matches(message, expected_text, value)

    expected: dict[str, Any] = (
        {"message": "any"} if message is None else {message.kind: expected_text}
    )
    return await _property(step, ctx, read, accept, expected, "message")


# --- the page -----------------------------------------------------------------------------


async def assert_url(step: dsl.AssertUrl, ctx: StepContext) -> StepOutcome:
    expected = ctx.values.render(step.url.text)

    async def read() -> str:
        return ctx.page.url

    ok, url = await poll(
        read,
        lambda u: matches(step.url, expected, url_subject(str(u), expected)),
        action_timeout(step, ctx),
    )
    wanted, actual = {step.url.kind: expected}, {"url": url}
    if not ok:
        raise _fail("Address", wanted, actual)
    return StepOutcome(expected=wanted, actual=actual)


async def assert_title(step: dsl.AssertTitle, ctx: StepContext) -> StepOutcome:
    expected = ctx.values.render(step.title.text)
    ok, title = await poll(
        ctx.page.title, lambda t: matches(step.title, expected, t), action_timeout(step, ctx)
    )
    wanted, actual = {step.title.kind: expected}, {"title": title}
    if not ok:
        raise _fail("Page title", wanted, actual)
    return StepOutcome(expected=wanted, actual=actual)


# --- API responses ------------------------------------------------------------------------


def _response(name: str, ctx: StepContext) -> dict[str, Any]:
    if name not in ctx.values.responses:
        raise StepError("test_data", f"No API response was saved as '{name}'.")
    return ctx.values.responses[name]


async def assert_api_status(step: dsl.AssertApiStatus, ctx: StepContext) -> StepOutcome:
    response = _response(step.response, ctx)
    allowed = step.status if isinstance(step.status, list) else [step.status]
    expected, actual = {"status": allowed}, {"status": response["status"]}
    if response["status"] not in allowed:
        raise _fail(f"Response '{step.response}'", expected, actual)
    return StepOutcome(expected=expected, actual=actual)


async def assert_api_json(step: dsl.AssertApiJson, ctx: StepContext) -> StepOutcome:
    response = _response(step.response, ctx)
    where = f"Response '{step.response}' at {step.pointer or '/'}"
    if not response["is_json"]:
        raise _fail(where, {"json": True}, {"json": False, "status": response["status"]})
    found, value = json_pointer(response["json"], step.pointer)
    expected: dict[str, Any]
    actual: dict[str, Any]
    if step.exists is not None:
        expected, actual = {"exists": step.exists}, {"exists": found}
        ok = found is step.exists
    elif not found:
        raise _fail(where, {"exists": True}, {"exists": False})
    elif step.type is not None:
        expected, actual = {"type": step.type}, {"type": json_type(value), "value": value}
        ok = json_type(value) == step.type
    elif step.text is not None:
        text = ctx.values.render(step.text.text)
        expected, actual = {step.text.kind: text}, {"value": value}
        ok = not isinstance(value, dict | list) and matches(step.text, text, value)
    else:
        wanted = ctx.values.render_any(step.equals)
        expected, actual = {"equals": wanted}, {"value": value}
        ok = _json_equal(value, wanted)
    if not ok:
        raise _fail(where, expected, actual)
    return StepOutcome(expected=expected, actual=actual)


def _json_equal(left: Any, right: Any) -> bool:
    """Equality as JSON has it: 1 equals 1.0, but true is not 1 and "50" is not 50."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, int | float) and isinstance(right, int | float):
        return left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_json_equal(left[k], right[k]) for k in left)
    return type(left) is type(right) and bool(left == right)


# --- business rules -----------------------------------------------------------------------


async def assert_expression(step: dsl.AssertExpression, ctx: StepContext) -> StepOutcome:
    values = ctx.values.for_expression()
    used = {
        name: str(values[name]) for name in sorted(names_used(step.expression)) if name in values
    }
    try:
        result = evaluate(step.expression, values)
    except ExpressionError as exc:
        raise StepError("test_data", f"The rule could not be evaluated: {exc}") from None
    if not isinstance(result, bool):
        raise StepError("test_data", "The rule must be a comparison that is true or false.")
    expected = {"rule": step.expression, "holds": True}
    actual = {"holds": result, "values": used}
    if not result:
        raise _fail("Rule", expected, actual)
    return StepOutcome(expected=expected, actual=actual)


Handler = Callable[[Any, StepContext], Awaitable[StepOutcome]]
ASSERTIONS: dict[str, Handler] = {
    "assert_visible": assert_visible,
    "assert_hidden": assert_hidden,
    "assert_exists": assert_exists,
    "assert_absent": assert_absent,
    "assert_count": assert_count,
    "assert_enabled": assert_enabled,
    "assert_disabled": assert_disabled,
    "assert_checked": assert_checked,
    "assert_text": assert_text,
    "assert_value": assert_value,
    "assert_validation_message": assert_validation_message,
    "assert_url": assert_url,
    "assert_title": assert_title,
    "assert_api_status": assert_api_status,
    "assert_api_json": assert_api_json,
    "assert_expression": assert_expression,
}
