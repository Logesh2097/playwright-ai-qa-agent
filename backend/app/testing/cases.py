"""The structured test case.

`CaseContent` is one model with three jobs: the JSON schema the LLM must fill,
the body a person submits when writing or editing a case, and the shape stored
per case version. Unknown fields are rejected in all three.

Steps name elements in words ("the Email field"), not with selectors. The agent
has not seen the application at this stage; real locators are attached when the
browser integration explores it. A guessed selector would only look precise.

As in requirement analysis, an expected result is either known and written
down, or `unknown` and empty. There is no third state in which a guess can sit.
"""

import re
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

MAX_TEXT = 1000
MAX_ITEMS = 40
MAX_STEPS = 40

Category = Literal[
    "positive",
    "negative",
    "boundary",
    "edge_case",
    "form_validation",
    "navigation",
    "authentication",
    "api_validation",
    "responsive",
    "business_logic",
]
TestType = Literal["ui", "api", "responsive"]
Priority = Literal["p0", "p1", "p2", "p3"]
Severity = Literal["critical", "major", "minor", "trivial"]
Feasibility = Literal["automatable", "partially_automatable", "manual_only"]
VerificationMethod = Literal[
    "ui_assertion", "api_response", "data_check", "visual_check", "manual_review"
]
ExpectedSource = Literal["plan_objective", "configured_rule", "human_specified", "unknown"]
SideEffect = Literal["none", "create", "update", "delete", "external"]
StepAction = Literal[
    "navigate",
    "click",
    "fill",
    "select",
    "check",
    "uncheck",
    "press",
    "hover",
    "upload",
    "wait_for",
    "set_viewport",
    "api_request",
    "assert_visible",
    "assert_hidden",
    "assert_text",
    "assert_value",
    "assert_url",
    "assert_enabled",
    "assert_disabled",
    "assert_status",
    "assert_json",
    "observe",
]

ASSERTION_ACTIONS = frozenset(a for a in StepAction.__args__ if a.startswith("assert_"))  # type: ignore[attr-defined]
# Steps that act on an element and therefore must say which one.
TARGETED_ACTIONS = frozenset(
    {"click", "fill", "select", "check", "uncheck", "hover", "upload"} | ASSERTION_ACTIONS
) - {"assert_url", "assert_status", "assert_json"}
# Steps that are meaningless without a value.
VALUE_ACTIONS = frozenset(
    {"navigate", "fill", "select", "press", "api_request", "assert_text", "assert_value",
     "assert_url", "assert_status", "set_viewport"}
)  # fmt: skip
# Which permission of an environment each action needs (see BROWSER_ACTIONS).
ACTION_PERMISSION = {
    "navigate": "navigate",
    "click": "click",
    "hover": "read",
    "fill": "type",
    "press": "type",
    "select": "select",
    "check": "click",
    "uncheck": "click",
    "upload": "upload",
    "wait_for": "read",
    "set_viewport": "read",
    "observe": "read",
    "api_request": "api_read",
}

PLACEHOLDER = re.compile(r"\{\{\s*(data|cred)\.([A-Za-z0-9_.-]+)\s*\}\}")
_DATA_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


class _Content(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CaseStep(_Content):
    action: StepAction
    # What the tester or the engine does, in plain words.
    description: str
    # The element, described the way a person would point at it. Null for steps without one.
    target: str | None
    # Text to type, option to choose, path to open, or the value an assertion compares with.
    # May contain {{data.NAME}} or {{cred.ROLE.FIELD}} placeholders.
    value: str | None
    side_effect: SideEffect

    @property
    def is_assertion(self) -> bool:
        return self.action in ASSERTION_ACTIONS


class TestDataItem(_Content):
    __test__ = False

    name: str
    # Null means the value still has to be chosen by a person.
    value: str | None
    description: str | None


class Verification(_Content):
    method: VerificationMethod
    # How a pass is told from a fail.
    description: str


class CaseContent(_Content):
    title: str
    description: str | None
    category: Category
    test_type: TestType
    # The plan objective this case verifies. Null for a scenario beyond the plan.
    objective_id: str | None
    preconditions: list[str]
    test_data: list[TestDataItem]
    steps: list[CaseStep]
    # What must be observed for the case to pass. Null exactly when the source is unknown.
    expected_result: str | None
    expected_source: ExpectedSource
    priority: Priority
    severity: Severity
    tags: list[str]
    # Titles or keys of cases that must pass first, or other things this case relies on.
    dependencies: list[str]
    automation_feasibility: Feasibility
    automation_notes: str | None
    verification: Verification
    # Anything the author took for granted without it being stated. Should be empty.
    assumptions: list[str]

    @model_validator(mode="after")
    def _check(self) -> Self:
        _check_sizes(self.model_dump())
        if len(self.steps) > MAX_STEPS:
            raise ValueError(f"a case has at most {MAX_STEPS} steps")
        if not self.title:
            raise ValueError("title is required")
        if self.expected_source == "unknown" and self.expected_result:
            raise ValueError(
                "expected_result must be null when expected_source is 'unknown': do not guess the result"
            )
        if self.expected_source != "unknown" and not self.expected_result:
            raise ValueError("expected_result is required unless expected_source is 'unknown'")
        names = [item.name for item in self.test_data]
        if len(set(names)) != len(names):
            raise ValueError("test data names must be unique within a case")
        if not all(_DATA_NAME.fullmatch(name) for name in names):
            raise ValueError("test data names must be identifiers such as bill_amount")
        return self

    def signature(self) -> tuple[Any, ...]:
        """What makes two cases the same scenario, ignoring wording and case."""
        return (
            tuple((s.action, _normalise(s.target), _normalise(s.value)) for s in self.steps),
            tuple(sorted((d.name.lower(), _normalise(d.value)) for d in self.test_data)),
        )

    def title_key(self) -> str:
        return _normalise(self.title)


class GeneratedCase(CaseContent):
    """A case as the model returns it."""

    # The model's own handle for the case within one answer, e.g. "C1".
    local_id: str


class GeneratedCases(_Content):
    cases: list[GeneratedCase]

    @model_validator(mode="after")
    def _limits(self) -> Self:
        if len(self.cases) > MAX_ITEMS:
            raise ValueError(f"at most {MAX_ITEMS} cases per answer")
        return self


def _normalise(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _check_sizes(value: Any, path: str = "") -> None:
    if isinstance(value, str) and len(value) > MAX_TEXT:
        raise ValueError(f"{path or 'text'} is longer than {MAX_TEXT} characters")
    if isinstance(value, list):
        if len(value) > MAX_ITEMS:
            raise ValueError(f"{path or 'list'} has more than {MAX_ITEMS} items")
        for index, item in enumerate(value):
            _check_sizes(item, f"{path}[{index}]")
    if isinstance(value, dict):
        for key, item in value.items():
            _check_sizes(item, f"{path}.{key}" if path else key)
