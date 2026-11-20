"""What a model returns when asked to write executable scripts, and how it becomes a `Script`.

The model fills a flat, fully explicit form (every field present, unused ones
null), which is what strict structured output supports. Code then builds the
real step for each action and validates it against the engine's schema. A
script that does not validate is not repaired or guessed at: it is reported.
"""

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from app.testing.automation import Script

Action = Literal[
    "navigate", "click", "fill", "clear", "select_option", "check", "uncheck", "select_radio",
    "upload_file", "wait_for_element", "wait_for_navigation", "read_text", "read_attribute",
    "screenshot", "api_request", "assert_visible", "assert_hidden", "assert_exists",
    "assert_absent", "assert_count", "assert_enabled", "assert_disabled", "assert_checked",
    "assert_text", "assert_value", "assert_validation_message", "assert_url", "assert_title",
    "assert_api_status", "assert_api_json", "assert_expression",
]  # fmt: skip
Strategy = Literal["role", "label", "text", "placeholder", "test_id", "css"]
Comparison = Literal["equals", "contains", "matches", "not_equals"]

MAX_SCRIPTS = 20


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GeneratedLocator(_Out):
    by: Strategy
    role: str | None
    name: str | None
    value: str | None
    exact: bool


class GeneratedStep(_Out):
    action: Action
    description: str | None
    locator: GeneratedLocator | None
    within: GeneratedLocator | None
    url: str | None
    # Text to type, option value to choose.
    value: str | None
    # Visible label of the option to choose.
    label: str | None
    # For comparisons: how, and with what.
    comparison: Comparison | None
    expected: str | None
    # wait_for_element
    state: Literal["visible", "hidden", "attached", "detached"] | None
    # read_* and api_request
    save_as: str | None
    as_number: bool | None
    attribute: str | None
    file: str | None
    # api_request
    method: Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"] | None
    json_body: str | None
    # API assertions
    response: str | None
    status: int | None
    pointer: str | None
    json_equals: str | None
    exists: bool | None
    # other assertions
    count: int | None
    checked: bool | None
    expression: str | None


class GeneratedScript(_Out):
    # The key of the case this script is for, e.g. "TC-0003".
    case_key: str
    login_as: str | None
    steps: list[GeneratedStep]
    cleanup: list[GeneratedStep]
    # Elements or values the model could not find in what it was shown.
    unresolved: list[str]


class GeneratedScripts(_Out):
    scripts: list[GeneratedScript]


class ScriptConversionError(ValueError):
    pass


def _locator(step: GeneratedStep) -> dict[str, Any]:
    if step.locator is None:
        raise ScriptConversionError(f"{step.action} needs a locator")

    def one(locator: GeneratedLocator) -> dict[str, Any]:
        built: dict[str, Any] = {"by": locator.by, "exact": locator.exact}
        if locator.by == "role":
            built["role"] = locator.role
            if locator.name:
                built["name"] = locator.name
        else:
            built["value"] = locator.value
        return built

    built = one(step.locator)
    if step.within is not None:
        built["within"] = one(step.within)
    return built


def _match(step: GeneratedStep) -> dict[str, Any]:
    if step.comparison is None or step.expected is None:
        raise ScriptConversionError(f"{step.action} needs comparison and expected")
    return {step.comparison: step.expected}


def _json(text: str | None, what: str) -> Any:
    try:
        return json.loads(text or "")
    except ValueError:
        raise ScriptConversionError(f"{what} is not valid JSON") from None


def _step(step: GeneratedStep) -> dict[str, Any]:
    action = step.action
    built: dict[str, Any] = {"action": action}
    if step.description:
        built["description"] = step.description
    if action == "navigate":
        built["url"] = step.url
    elif action in (
        "click", "clear", "check", "uncheck", "select_radio", "assert_visible", "assert_hidden",
        "assert_exists", "assert_absent", "assert_enabled", "assert_disabled",
    ):  # fmt: skip
        built["locator"] = _locator(step)
    elif action == "fill":
        built |= {"locator": _locator(step), "value": step.value}
    elif action == "select_option":
        built["locator"] = _locator(step)
        built |= {"label": step.label} if step.label is not None else {"value": step.value}
    elif action == "upload_file":
        built |= {"locator": _locator(step), "file": step.file}
    elif action == "wait_for_element":
        built |= {"locator": _locator(step), "state": step.state or "visible"}
    elif action == "wait_for_navigation":
        built["url"] = _match(step)
    elif action == "read_text":
        built |= {
            "locator": _locator(step),
            "save_as": step.save_as,
            "as_number": bool(step.as_number),
        }
    elif action == "read_attribute":
        built |= {"locator": _locator(step), "attribute": step.attribute, "save_as": step.save_as}
    elif action == "api_request":
        built |= {"method": step.method or "GET", "url": step.url, "save_as": step.save_as}
        if step.json_body is not None:
            built["json_body"] = _json(step.json_body, "json_body")
    elif action == "assert_count":
        built |= {"locator": _locator(step), "count": step.count}
    elif action == "assert_checked":
        built |= {"locator": _locator(step), "checked": step.checked is not False}
    elif action == "assert_text":
        built |= {"locator": _locator(step), "text": _match(step)}
    elif action == "assert_value":
        built |= {"locator": _locator(step), "value": _match(step)}
    elif action == "assert_validation_message":
        built["locator"] = _locator(step)
        if step.comparison is not None and step.expected is not None:
            built["message"] = _match(step)
    elif action == "assert_url":
        built["url"] = _match(step)
    elif action == "assert_title":
        built["title"] = _match(step)
    elif action == "assert_api_status":
        built |= {"response": step.response, "status": step.status}
    elif action == "assert_api_json":
        built |= {"response": step.response, "pointer": step.pointer or ""}
        if step.json_equals is not None:
            built["equals"] = _json(step.json_equals, "json_equals")
        elif step.exists is not None:
            built["exists"] = step.exists
        else:
            built["text"] = _match(step)
    elif action == "assert_expression":
        built["expression"] = step.expression
    return built


def to_script(generated: GeneratedScript) -> Script:
    """Build and validate the engine's script. Raises `ScriptConversionError` with the reason."""
    try:
        return Script.model_validate(
            {
                "login_as": generated.login_as,
                "steps": [_step(step) for step in generated.steps],
                "cleanup": [_step(step) for step in generated.cleanup],
            }
        )
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        raise ScriptConversionError(f"{where}: {first['msg']}") from None
