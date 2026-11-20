"""Checks on a script before it may run, made without a browser.

They catch what can be known in advance: a placeholder nothing will fill, an
address outside the environment, an action the environment forbids, a value
read before it was saved. What only the application can answer (does this
button exist?) is found by running the script.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from pydantic import BaseModel

from app.core.security.url_safety import Origin, UrlNotAllowedError, parse_origin
from app.testing.automation import (
    PLACEHOLDER,
    Script,
    changes_state,
    is_assertion,
    malformed_placeholders,
    permission_for,
    placeholders_in,
)
from app.testing.case_validation import CaseIssue
from app.testing.expressions import names_used

UNIQUE_NAME = re.compile(r"^(id|email|number|text)(_[a-z0-9_]{1,30})?$")
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


@dataclass(frozen=True, slots=True)
class ScriptFacts:
    """What a script is checked against."""

    allowed_origins: frozenset[Origin]
    allow_http: bool = True
    read_only: bool = False
    allowed_actions: frozenset[str] = frozenset()
    env_names: frozenset[str] = frozenset()
    data_names: frozenset[str] = frozenset()
    data_set_names: frozenset[str] = frozenset()
    # role label -> names of the credential's fields
    credentials: Mapping[str, frozenset[str]] = field(default_factory=dict)
    # role labels whose credential has a usable login recipe
    login_roles: frozenset[str] = frozenset()
    file_names: frozenset[str] = frozenset()


def check_script(script: Script, facts: ScriptFacts) -> list[CaseIssue]:
    issues: list[CaseIssue] = []

    def add(code: str, message: str, step: int | None = None, *, blocking: bool = True) -> None:
        issues.append(CaseIssue(code, "blocking" if blocking else "warning", message, step))

    if script.login_as is not None and script.login_as not in facts.login_roles:
        reason = (
            "has no usable login recipe"
            if script.login_as in facts.credentials
            else "does not exist in this environment"
        )
        add(
            "automation_login_unavailable",
            f"login_as: the credential '{script.login_as}' {reason}.",
        )
    for name in script.data_sets:
        if name not in facts.data_set_names:
            add(
                "automation_data_set_unknown",
                f"The data set '{name}' does not exist for this project.",
            )
    if not any(is_assertion(step) for step in script.steps):
        add(
            "automation_no_assertion",
            "The script checks nothing. Add at least one assertion, or it would pass whatever happens.",
        )

    saved: set[str] = set()
    responses: set[str] = set()
    steps: list[tuple[int, str, BaseModel]] = [
        *((n, f"Step {n}", s) for n, s in enumerate(script.steps, start=1)),
        *(
            (len(script.steps) + n, f"Cleanup step {n}", s)
            for n, s in enumerate(script.cleanup, start=1)
        ),
    ]
    for number, label, step in steps:
        action = str(getattr(step, "action", ""))
        for found in malformed_placeholders(step):
            add(
                "automation_placeholder_invalid",
                f"{label}: {found} is not a known placeholder.",
                number,
            )
        for namespace, name in placeholders_in(step):
            problem = _placeholder_problem(namespace, name, facts, saved)
            if problem:
                add("automation_placeholder_unknown", f"{label}: {problem}", number)

        permission = permission_for(step)
        if permission not in facts.allowed_actions:
            add(
                "automation_action_not_allowed",
                f"{label}: the environment does not allow '{permission}' actions.",
                number,
            )
        elif facts.read_only and changes_state(step):
            add(
                "automation_environment_read_only",
                f"{label}: '{action}' can change data, and the environment is read-only.",
                number,
            )

        url = getattr(step, "url", None)
        if isinstance(url, str):
            problem = _url_problem(url, facts)
            if problem:
                add("automation_url_not_allowed", f"{label}: {problem}", number)
        if action == "upload_file" and getattr(step, "file", None) not in facts.file_names:
            add(
                "automation_file_unknown",
                f"{label}: there is no test file named '{getattr(step, 'file', '')}' in this project.",
                number,
            )
        if action in ("assert_api_status", "assert_api_json"):
            response = str(getattr(step, "response", ""))
            if response not in responses:
                add(
                    "automation_response_unknown",
                    f"{label}: no earlier api_request saves a response as '{response}'.",
                    number,
                )
        if action == "assert_expression":
            for used in sorted(names_used(str(getattr(step, "expression", "")))):
                namespace, _, name = used.partition(".")
                problem = _placeholder_problem(namespace, name, facts, saved)
                if problem:
                    add("automation_placeholder_unknown", f"{label}: {problem}", number)

        locator = getattr(step, "locator", None)
        if locator is not None and getattr(locator, "by", None) == "css":
            add(
                "automation_fragile_locator",
                f"{label}: a CSS selector breaks when the page's markup changes. "
                "Prefer role, label or test_id where the element has one.",
                number,
                blocking=False,
            )

        save_as = getattr(step, "save_as", None)
        if isinstance(save_as, str):
            if action == "api_request":
                responses.add(save_as)
            else:
                saved.add(save_as)
    return issues


def _placeholder_problem(
    namespace: str, name: str, facts: ScriptFacts, saved: set[str]
) -> str | None:
    if namespace == "env":
        return None if name in facts.env_names else f"the environment has no variable '{name}'."
    if namespace == "data":
        return None if name in facts.data_names else f"there is no test data named '{name}'."
    if namespace == "var":
        return None if name in saved else f"no earlier step saves a value as '{name}'."
    if namespace == "unique":
        if UNIQUE_NAME.fullmatch(name):
            return None
        return f"unique.{name}: use id, email, number or text, optionally with a suffix (email_second)."
    role, _, field_name = name.partition(".")
    if role not in facts.credentials:
        return f"there is no credential with the role '{role}' in this environment."
    if field_name not in facts.credentials[role]:
        return f"the credential '{role}' has no field '{field_name}'."
    return None


def _url_problem(url: str, facts: ScriptFacts) -> str | None:
    if url.startswith("/") and not url.startswith("//"):
        return None
    if PLACEHOLDER.match(url):
        # Starts with a placeholder: the address is only known at run time, where it
        # is checked again before anything is requested.
        return None
    if not _SCHEME.match(url):
        return "the address must be a path starting with / or a full address."
    try:
        origin = parse_origin(PLACEHOLDER.sub("x", url), allow_http=facts.allow_http)
    except UrlNotAllowedError as exc:
        return exc.message
    if origin not in facts.allowed_origins:
        return f"{origin} is not in the environment's list of allowed origins."
    return None
