"""Signing in to the application under test with a stored test credential.

The steps come from the credential's login recipe, which a person wrote. No
model is involved: nothing here reads the page to decide what to do, so text on
the page cannot change what happens, and the secret values never enter a
prompt. Values are filled in at the moment of the call and registered with the
session first, so that results, logs and errors show them as `[REDACTED]`.

A login counts as successful only if every step ran and every `expect` step
held. There is no partial success.
"""

import asyncio
import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from app.browser.errors import BrowserError, LoginFailedError
from app.browser.session import BrowserSession

_PLACEHOLDER = re.compile(r"^\{\{cred\.([a-z_][a-z0-9_]{0,40})\}\}$")
# Credential fields that identify the account rather than prove it. They stay
# readable in logs; every other field is treated as secret.
_IDENTIFIER_FIELDS = frozenset({"username", "email", "login", "header_name", "name"})
_EXPECT_ATTEMPTS = 10

LOGIN_TOOLS = frozenset(
    {"browser_navigate", "browser_type", "browser_click", "browser_wait_for", "browser_snapshot"}
)
LOGIN_ACTIONS = frozenset({"navigate", "read", "type", "click"})

Role = Literal["textbox", "button", "link", "checkbox", "combobox", "searchbox", "menuitem", "tab"]


class _Step(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Target(_Step):
    """An element identified the way a user would: by its role and its visible name."""

    role: Role
    name: str = Field(min_length=1, max_length=200)

    def selector(self) -> str:
        escaped = self.name.replace("\\", "\\\\").replace('"', '\\"')
        return f'role={self.role}[name="{escaped}"]'


class GotoStep(_Step):
    action: Literal["goto"]
    path: str = Field(pattern=r"^/[^\s]*$", max_length=500, description="Path on the environment.")


class FillStep(_Step):
    action: Literal["fill"]
    target: Target
    value: str = Field(description="A credential field, written as {{cred.username}}.")

    @model_validator(mode="after")
    def _placeholder_only(self) -> Self:
        if not _PLACEHOLDER.fullmatch(self.value):
            raise ValueError("value must be one placeholder such as {{cred.password}}")
        return self

    @property
    def field(self) -> str:
        match = _PLACEHOLDER.fullmatch(self.value)
        assert match is not None  # noqa: S101 - validated above
        return match.group(1)


class ClickStep(_Step):
    action: Literal["click"]
    target: Target


class ExpectStep(_Step):
    """What must be true once signed in. At least one of the two."""

    action: Literal["expect"]
    text: str | None = Field(default=None, min_length=1, max_length=200)
    url_path: str | None = Field(default=None, pattern=r"^/[^\s]*$", max_length=500)

    @model_validator(mode="after")
    def _something_to_check(self) -> Self:
        if self.text is None and self.url_path is None:
            raise ValueError("give text or url_path")
        return self


LoginStep = Annotated[GotoStep | FillStep | ClickStep | ExpectStep, Field(discriminator="action")]


class LoginRecipe(RootModel[list[LoginStep]]):
    @model_validator(mode="after")
    def _complete(self) -> Self:
        steps = self.root
        if not 1 <= len(steps) <= 50:
            raise ValueError("a login recipe has between 1 and 50 steps")
        if not isinstance(steps[0], GotoStep):
            raise ValueError("the first step must be a goto step")
        if not any(isinstance(step, FillStep) for step in steps):
            raise ValueError("a login recipe needs at least one fill step")
        if not isinstance(steps[-1], ExpectStep):
            raise ValueError(
                "the last step must be an expect step, so that a failed login is noticed"
            )
        return self

    def fields(self) -> set[str]:
        return {step.field for step in self.root if isinstance(step, FillStep)}

    def dump(self) -> list[dict[str, Any]]:
        return [step.model_dump(exclude_none=True) for step in self.root]


@dataclass(frozen=True, slots=True)
class LoginResult:
    steps_performed: int
    page_url: str | None
    page_title: str | None


def secret_values(fields: dict[str, str]) -> list[str]:
    return [value for name, value in fields.items() if name not in _IDENTIFIER_FIELDS and value]


async def run_login(
    session: BrowserSession, *, base_url: str, recipe: LoginRecipe, fields: dict[str, str]
) -> LoginResult:
    """Perform the recipe. Raises `LoginFailedError` unless every step succeeded."""
    missing = sorted(recipe.fields() - set(fields))
    if missing:
        raise LoginFailedError(
            f"The credential has no field named {', '.join(missing)}.", code="login_recipe_invalid"
        )
    session.add_secrets(*secret_values(fields))
    parts = urlsplit(base_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    performed = 0
    last_url: str | None = None
    last_title: str | None = None
    for number, step in enumerate(recipe.root, start=1):
        try:
            if isinstance(step, GotoStep):
                outcome = await session.call("browser_navigate", {"url": origin + step.path})
            elif isinstance(step, FillStep):
                outcome = await session.call(
                    "browser_type", {"target": step.target.selector(), "text": fields[step.field]}
                )
            elif isinstance(step, ClickStep):
                outcome = await session.call("browser_click", {"target": step.target.selector()})
            else:
                outcome = None
                last_url, last_title = await _expect(session, step, number)
        except LoginFailedError:
            raise
        except BrowserError as exc:
            raise LoginFailedError(
                f"Login step {number} ({step.action}) failed: {session.redact(exc.message)}",
                code=exc.code,
            ) from None
        if outcome is not None and outcome.page_url:
            last_url, last_title = outcome.page_url, outcome.page_title
        performed += 1
    return LoginResult(performed, last_url, last_title)


async def _expect(
    session: BrowserSession, step: ExpectStep, number: int
) -> tuple[str | None, str | None]:
    if step.text is not None:
        try:
            await session.call("browser_wait_for", {"text": step.text, "time": 10})
        except BrowserError as exc:
            if exc.code not in ("browser_timeout", "tool_failed"):
                raise
            raise LoginFailedError(
                f"Login step {number}: the expected text did not appear. "
                "The credential may be wrong, or the page changed.",
            ) from None
    url: str | None = None
    title: str | None = None
    for attempt in range(_EXPECT_ATTEMPTS):
        outcome = await session.call("browser_snapshot", {"depth": 1})
        url, title = outcome.page_url, outcome.page_title
        if step.url_path is None:
            break
        if url is not None and urlsplit(url).path == step.url_path:
            break
        if attempt == _EXPECT_ATTEMPTS - 1:
            raise LoginFailedError(
                f"Login step {number}: the browser did not arrive at {step.url_path}. "
                "The credential may be wrong, or the page changed.",
            )
        await asyncio.sleep(0.5)
    return url, title
