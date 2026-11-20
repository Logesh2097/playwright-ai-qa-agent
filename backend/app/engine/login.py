"""Signing in before a case, from the credential's login recipe.

Done in a context of its own with no video and no trace, so the password is
never recorded. What is carried into the case is the resulting session
(cookies and storage), held in memory for that case only.
"""

from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.browser.login import ClickStep, ExpectStep, FillStep, GotoStep, LoginRecipe
from app.engine.browser import Scope
from app.engine.errors import StepError
from app.engine.protocol import Timeouts
from app.engine.steps import first_line


def _at_path(path: str) -> Callable[[str], bool]:
    return lambda url: (urlsplit(url).path or "/") == path


async def sign_in(
    context: BrowserContext,
    scope: Scope,
    recipe: LoginRecipe,
    fields: dict[str, str],
    timeouts: Timeouts,
) -> Any:
    """Perform the recipe and return the session state. Raises `StepError` on any doubt."""
    missing = sorted(recipe.fields() - set(fields))
    if missing:
        raise StepError("login_failed", f"The credential has no field named {', '.join(missing)}.")
    page = await context.new_page()
    for number, step in enumerate(recipe.root, start=1):
        try:
            if isinstance(step, GotoStep):
                url = scope.absolute(step.path)
                scope.check(url)
                await page.goto(url)
            elif isinstance(step, FillStep):
                await page.get_by_role(step.target.role, name=step.target.name).fill(
                    fields[step.field]
                )
            elif isinstance(step, ClickStep):
                await page.get_by_role(step.target.role, name=step.target.name).click()
            elif isinstance(step, ExpectStep):
                if step.text is not None:
                    await page.get_by_text(step.text).first.wait_for(
                        state="visible", timeout=timeouts.navigation_ms
                    )
                if step.url_path is not None:
                    await page.wait_for_url(_at_path(step.url_path), timeout=timeouts.navigation_ms)
        except PlaywrightTimeoutError:
            raise StepError(
                "login_failed",
                f"Sign-in step {number} ({step.action}) did not succeed in time. "
                "The credential may be wrong, or the page changed.",
            ) from None
        except PlaywrightError as exc:
            raise StepError(
                "login_failed", f"Sign-in step {number} ({step.action}): {first_line(exc)}"
            ) from None
    scope.check(page.url)
    return await context.storage_state()
