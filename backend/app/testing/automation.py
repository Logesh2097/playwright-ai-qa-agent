"""The executable form of a test case: a script of strictly typed steps.

A case has two descriptions. `CaseContent` (see `cases.py`) says in words what
is tested, for people to review. A `Script` says exactly what the test engine
does: which element, which action, which value, which assertion. The engine
executes scripts and nothing else, so everything it can do is listed here.

Every step is validated against its own schema with unknown fields rejected.
There is no step that runs code, evaluates JavaScript, sleeps for a fixed time
or takes a raw Playwright selector string.

Values may contain placeholders, resolved by the engine at the moment of use:

    {{env.NAME}}          a variable of the environment
    {{data.NAME}}         test data of the case or of a data set
    {{cred.ROLE.FIELD}}   a field of a stored test credential
    {{unique.NAME}}       a value generated for this run (also .email, .number)
    {{var.NAME}}          a value an earlier step saved
"""

import re
from collections.abc import Iterator
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.testing.expressions import ExpressionError, parse

MAX_STEPS = 100
MAX_CLEANUP_STEPS = 30

PLACEHOLDER = re.compile(r"\{\{\s*(env|data|cred|unique|var)\.([A-Za-z0-9_.-]+)\s*\}\}")
_ANY_BRACES = re.compile(r"\{\{.*?\}\}")
_ENGINE_PREFIX = re.compile(r"^\s*[a-z_-]+\s*=", re.IGNORECASE)

Name = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,40}$")]
Short = Annotated[str, StringConstraints(min_length=1, max_length=500)]
Value = Annotated[str, StringConstraints(max_length=5000)]
TimeoutMs = Annotated[int, Field(ge=100, le=120_000)]

Role = Literal[
    "alert", "alertdialog", "article", "banner", "button", "cell", "checkbox", "columnheader",
    "combobox", "dialog", "form", "group", "heading", "img", "link", "list", "listbox",
    "listitem", "main", "menu", "menuitem", "navigation", "option", "progressbar", "radio",
    "radiogroup", "region", "row", "rowheader", "searchbox", "slider", "spinbutton", "status",
    "switch", "tab", "table", "tabpanel", "textbox", "tooltip",
]  # fmt: skip


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- locators -----------------------------------------------------------------------------


class Locator(_Model):
    """How to find one element. Must match exactly one; more or none is an error.

    Preferred, in this order: `role` with the accessible name, `label`, `test_id`,
    `placeholder`, `text`. `css` exists for elements nothing else can reach.
    XPath is not accepted.
    """

    by: Literal["role", "label", "text", "placeholder", "test_id", "css"]
    role: Role | None = None
    name: Short | None = Field(default=None, description="Accessible name, with by=role.")
    value: Short | None = Field(
        default=None, description="Label, text, placeholder, ID or selector."
    )
    exact: bool = False
    within: "Locator | None" = Field(default=None, description="Search only inside this element.")

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.by == "role":
            if self.role is None or self.value is not None:
                raise ValueError("by=role needs role (and optionally name), not value")
        elif self.value is None or self.role is not None or self.name is not None:
            raise ValueError(f"by={self.by} needs value, not role or name")
        if self.by == "css":
            selector = self.value or ""
            if (
                selector.lstrip().startswith(("/", "(", ".."))
                or ">>" in selector
                or _ENGINE_PREFIX.match(selector)
            ):
                raise ValueError(
                    "css takes a plain CSS selector: XPath, chained selectors and "
                    "engine prefixes such as text= are not accepted"
                )
        if self.within is not None and self.within.within is not None:
            raise ValueError("within can be nested one level only")
        return self

    def describe(self) -> str:
        own = (
            f'{self.role} "{self.name}"'
            if self.by == "role" and self.name
            else str(self.role or self.value)
        )
        text = own if self.by == "role" else f"{self.by} {own!r}"
        return f"{text} within {self.within.describe()}" if self.within else text


class TextMatch(_Model):
    """How a text is compared. Exactly one of the four."""

    equals: Value | None = None
    contains: Value | None = None
    matches: Short | None = Field(default=None, description="Regular expression.")
    not_equals: Value | None = None
    ignore_case: bool = False

    @model_validator(mode="after")
    def _one(self) -> Self:
        given = [
            n
            for n in ("equals", "contains", "matches", "not_equals")
            if getattr(self, n) is not None
        ]
        if len(given) != 1:
            raise ValueError("give exactly one of equals, contains, matches, not_equals")
        if self.matches is not None and not _ANY_BRACES.search(self.matches):
            try:
                re.compile(self.matches)
            except re.error as exc:
                raise ValueError(f"matches is not a valid regular expression: {exc}") from None
        return self

    @property
    def kind(self) -> str:
        return next(
            n
            for n in ("equals", "contains", "matches", "not_equals")
            if getattr(self, n) is not None
        )

    @property
    def text(self) -> str:
        return str(getattr(self, self.kind))


# --- steps --------------------------------------------------------------------------------


class _Step(_Model):
    description: Short | None = Field(default=None, description="What this step is for.")
    timeout_ms: TimeoutMs | None = None


class _Assertion(_Step):
    # Record the failure and carry on, so one run reports several problems.
    continue_on_failure: bool = False


class Navigate(_Step):
    action: Literal["navigate"]
    url: Short = Field(description="A path on the environment (/login), or a full allowed address.")


class Click(_Step):
    action: Literal["click"]
    locator: Locator


class Fill(_Step):
    action: Literal["fill"]
    locator: Locator
    value: Value


class Clear(_Step):
    action: Literal["clear"]
    locator: Locator


class SelectOption(_Step):
    action: Literal["select_option"]
    locator: Locator
    value: Short | None = None
    label: Short | None = None

    @model_validator(mode="after")
    def _one(self) -> Self:
        if (self.value is None) == (self.label is None):
            raise ValueError("give either value or label")
        return self


class Check(_Step):
    action: Literal["check"]
    locator: Locator


class Uncheck(_Step):
    action: Literal["uncheck"]
    locator: Locator


class SelectRadio(_Step):
    action: Literal["select_radio"]
    locator: Locator


class UploadFile(_Step):
    action: Literal["upload_file"]
    locator: Locator
    file: Short = Field(description="Name of a test file of the project.")


class WaitForElement(_Step):
    action: Literal["wait_for_element"]
    locator: Locator
    state: Literal["visible", "hidden", "attached", "detached"] = "visible"


class WaitForNavigation(_Step):
    action: Literal["wait_for_navigation"]
    url: TextMatch = Field(
        description="Compared with the path, or the full address if it has a scheme."
    )


class ReadText(_Step):
    action: Literal["read_text"]
    locator: Locator
    save_as: Name
    as_number: bool = Field(
        default=False, description='Keep only the number: "₹1,250.50" becomes 1250.50.'
    )


class ReadAttribute(_Step):
    action: Literal["read_attribute"]
    locator: Locator
    attribute: Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_:-]{0,60}$")]
    save_as: Name


class Screenshot(_Step):
    action: Literal["screenshot"]
    name: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,40}$")] | None = None
    full_page: bool = False


class ApiRequest(_Step):
    action: Literal["api_request"]
    method: Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    url: Short
    headers: dict[Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9-]{1,64}$")], Value] = (
        Field(default_factory=dict, max_length=20)
    )
    json_body: Any | None = Field(
        default=None, description="Sent as JSON. Text inside may use placeholders."
    )
    save_as: Name = Field(description="Name under which later steps refer to the response.")


class AssertVisible(_Assertion):
    action: Literal["assert_visible"]
    locator: Locator


class AssertHidden(_Assertion):
    action: Literal["assert_hidden"]
    locator: Locator


class AssertExists(_Assertion):
    action: Literal["assert_exists"]
    locator: Locator


class AssertAbsent(_Assertion):
    action: Literal["assert_absent"]
    locator: Locator


class AssertCount(_Assertion):
    action: Literal["assert_count"]
    locator: Locator
    count: Annotated[int, Field(ge=0, le=10_000)]


class AssertEnabled(_Assertion):
    action: Literal["assert_enabled"]
    locator: Locator


class AssertDisabled(_Assertion):
    action: Literal["assert_disabled"]
    locator: Locator


class AssertChecked(_Assertion):
    action: Literal["assert_checked"]
    locator: Locator
    checked: bool = True


class AssertText(_Assertion):
    action: Literal["assert_text"]
    locator: Locator
    text: TextMatch


class AssertValue(_Assertion):
    action: Literal["assert_value"]
    locator: Locator
    value: TextMatch


class AssertValidationMessage(_Assertion):
    """The browser's own validation message of a form field (required, type, range…)."""

    action: Literal["assert_validation_message"]
    locator: Locator
    message: TextMatch | None = Field(default=None, description="Omit to require any message.")


class AssertUrl(_Assertion):
    action: Literal["assert_url"]
    url: TextMatch = Field(
        description="Compared with the path, or the full address if it has a scheme."
    )


class AssertTitle(_Assertion):
    action: Literal["assert_title"]
    title: TextMatch


class AssertApiStatus(_Assertion):
    action: Literal["assert_api_status"]
    response: Name
    status: Annotated[int, Field(ge=100, le=599)] | list[Annotated[int, Field(ge=100, le=599)]]


class AssertApiJson(_Assertion):
    """Check one place in a JSON response, addressed by JSON Pointer (/items/0/price)."""

    action: Literal["assert_api_json"]
    response: Name
    pointer: Annotated[str, StringConstraints(pattern=r"^(/[^/]*)*$", max_length=300)]
    equals: Any | None = None
    text: TextMatch | None = None
    exists: bool | None = None
    type: Literal["string", "number", "boolean", "array", "object", "null"] | None = None

    @model_validator(mode="after")
    def _one(self) -> Self:
        given = [
            n
            for n in ("equals", "text", "exists", "type")
            if n in self.model_fields_set and getattr(self, n) is not None
        ]
        # Comparing with JSON null is legitimate, and looks the same as "not given" once a
        # script has been written out in full. It is the check only if nothing else is.
        if "equals" in self.model_fields_set and self.equals is None and not given:
            given.append("equals")
        if len(given) != 1:
            raise ValueError("give exactly one of equals, text, exists, type")
        return self


class AssertExpression(_Assertion):
    """A business rule over saved values, e.g. `points == floor(bill_amount / 10)`."""

    action: Literal["assert_expression"]
    expression: Short

    @model_validator(mode="after")
    def _parses(self) -> Self:
        try:
            parse(self.expression)
        except ExpressionError as exc:
            raise ValueError(str(exc)) from None
        return self


Step = Annotated[
    Navigate | Click | Fill | Clear | SelectOption | Check | Uncheck | SelectRadio | UploadFile
    | WaitForElement | WaitForNavigation | ReadText | ReadAttribute | Screenshot | ApiRequest
    | AssertVisible | AssertHidden | AssertExists | AssertAbsent | AssertCount | AssertEnabled
    | AssertDisabled | AssertChecked | AssertText | AssertValue | AssertValidationMessage
    | AssertUrl | AssertTitle | AssertApiStatus | AssertApiJson | AssertExpression,
    Field(discriminator="action"),
]  # fmt: skip

# The environment action (see BROWSER_ACTIONS) each step needs. API requests depend on the method.
STEP_PERMISSION: dict[str, str] = {
    "navigate": "navigate",
    "click": "click",
    "check": "click",
    "uncheck": "click",
    "select_radio": "click",
    "fill": "type",
    "clear": "type",
    "select_option": "select",
    "upload_file": "upload",
}
# Steps that can change the application's data. Refused on read-only environments.
STATE_CHANGING = frozenset(STEP_PERMISSION) - {"navigate"}
SAFE_API_METHODS = frozenset({"GET", "HEAD"})


def permission_for(step: BaseModel) -> str:
    action = str(getattr(step, "action", ""))
    if action == "api_request":
        return "api_read" if getattr(step, "method", "GET") in SAFE_API_METHODS else "api_write"
    return STEP_PERMISSION.get(action, "read")


def changes_state(step: BaseModel) -> bool:
    action = str(getattr(step, "action", ""))
    if action == "api_request":
        return getattr(step, "method", "GET") not in SAFE_API_METHODS
    return action in STATE_CHANGING


def is_assertion(step: BaseModel) -> bool:
    return isinstance(step, _Assertion)


class Script(_Model):
    """Everything the engine does for one case."""

    login_as: Short | None = Field(
        default=None,
        description="Role label of a credential: sign in with it before the first step.",
    )
    data_sets: list[Short] = Field(default_factory=list, max_length=10)
    steps: list[Step] = Field(min_length=1, max_length=MAX_STEPS)
    # Always run after the steps, also when they failed, to undo what the test created.
    cleanup: list[Step] = Field(default_factory=list, max_length=MAX_CLEANUP_STEPS)

    @model_validator(mode="after")
    def _cleanup_has_no_verdict(self) -> Self:
        if any(is_assertion(step) for step in self.cleanup):
            raise ValueError("cleanup steps undo things; assertions belong in steps")
        return self


def strings_in(value: Any) -> Iterator[str]:
    """Every string inside a step, however deeply nested."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, BaseModel):
        for name in type(value).model_fields:
            yield from strings_in(getattr(value, name))
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from strings_in(key)
            yield from strings_in(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from strings_in(item)


def placeholders_in(value: Any) -> list[tuple[str, str]]:
    """(namespace, name) for each placeholder inside a step."""
    return [match.groups() for text in strings_in(value) for match in PLACEHOLDER.finditer(text)]  # type: ignore[misc]


def malformed_placeholders(value: Any) -> list[str]:
    """Things in double braces that are not one of the placeholders above."""
    return [
        found
        for text in strings_in(value)
        for found in _ANY_BRACES.findall(text)
        if not PLACEHOLDER.fullmatch(found)
    ]
