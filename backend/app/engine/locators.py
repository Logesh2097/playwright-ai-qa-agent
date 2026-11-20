"""Locator management: from a description to exactly one element.

Every strategy maps onto Playwright's own user-facing locators. A locator that
matches no element, or more than one, is reported as such: the engine never
picks "the first one".
"""

import contextlib
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator as Element
from playwright.async_api import Page

from app.engine.values import Values
from app.testing.automation import Locator


def find(page: Page, locator: Locator, values: Values) -> Element:
    root: Page | Element = find(page, locator.within, values) if locator.within else page
    text = values.render(locator.value) if locator.value is not None else ""
    if locator.by == "role":
        assert locator.role is not None  # noqa: S101 - guaranteed by the schema
        name = values.render(locator.name) if locator.name is not None else None
        if name is None:
            return root.get_by_role(locator.role)
        return root.get_by_role(locator.role, name=name, exact=locator.exact)
    if locator.by == "label":
        return root.get_by_label(text, exact=locator.exact)
    if locator.by == "text":
        return root.get_by_text(text, exact=locator.exact)
    if locator.by == "placeholder":
        return root.get_by_placeholder(text, exact=locator.exact)
    if locator.by == "test_id":
        return root.get_by_test_id(text)
    # The css= prefix pins the selector engine, so the text cannot switch to another one.
    return root.locator(f"css={text}")


async def matched(element: Element) -> int | None:
    """How many elements the locator matches right now. None if the page cannot say."""
    with contextlib.suppress(PlaywrightError):
        return await element.count()
    return None


def record(locator: Locator, values: Values, count: int | None) -> dict[str, Any]:
    """What is stored about the locator a step used."""
    entry: dict[str, Any] = {"by": locator.by, "matched": count}
    if locator.role is not None:
        entry["role"] = locator.role
    if locator.name is not None:
        entry["name"] = values.render(locator.name)
    if locator.value is not None:
        entry["value"] = values.render(locator.value)
    if locator.exact:
        entry["exact"] = True
    if locator.within is not None:
        entry["within"] = record(locator.within, values, None)
    return entry
