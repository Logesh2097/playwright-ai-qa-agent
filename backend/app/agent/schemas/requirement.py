"""The structured requirement an instruction is turned into.

This model is the contract in both directions: it is the JSON schema the LLM
must fill (in strict mode), and it validates whatever comes back, or whatever a
person submits when editing an analysis by hand. Unknown fields are rejected.

Two rules protect against invented results:

- every expected behaviour carries a `source`. With source `unknown` its
  description must be empty; with any other source it must be present;
- size limits are enforced here rather than in the JSON schema, because strict
  mode does not accept length keywords.
"""

import re
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

MAX_TEXT = 1000
MAX_ITEMS = 40
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")

# Where a statement comes from. `unknown` means: nobody said, so it must be asked.
Source = Literal[
    "stated_in_instruction", "provided_configuration", "clarification_answer", "unknown"
]
ObjectiveCategory = Literal["positive", "negative", "boundary", "edge_case", "validation"]
TestDataKind = Literal["credential", "input_value", "existing_record", "configuration_value"]


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ApplicationContext(_Output):
    application: str
    module: str | None
    user_actions: list[str]


class TestObjective(_Output):
    __test__ = False

    id: str
    title: str
    category: ObjectiveCategory


class ExpectedBehavior(_Output):
    objective_id: str
    # What must happen. Null exactly when the source is unknown.
    description: str | None
    source: Source


class BusinessRule(_Output):
    name: str
    description: str
    source: Source
    # False when the rule depends on a value nobody has supplied (e.g. "the configured minimum").
    value_known: bool


class TestDataRequirement(_Output):
    __test__ = False

    name: str
    kind: TestDataKind
    description: str
    # For kind "credential": the role the account plays, e.g. "customer".
    credential_role: str | None
    # Concrete values given in the instruction or configuration. Never invented.
    values: list[str]
    source: Source


class MissingInformation(_Output):
    topic: str
    why_needed: str
    blocking: bool


class ClarificationQuestion(_Output):
    id: str
    question: str
    options: list[str]
    related_to: str | None


class RequirementAnalysis(_Output):
    summary: str
    feature_name: str | None
    is_feature_identifiable: bool
    application_context: ApplicationContext
    preconditions: list[str]
    test_objectives: list[TestObjective]
    expected_behavior: list[ExpectedBehavior]
    business_rules: list[BusinessRule]
    validation_requirements: list[str]
    negative_scenarios: list[str]
    edge_cases: list[str]
    required_test_data: list[TestDataRequirement]
    dependencies: list[str]
    constraints: list[str]
    assumptions: list[str]
    missing_information: list[MissingInformation]
    clarification_questions: list[ClarificationQuestion]
    # Whether carrying out the objectives would create, change or delete data.
    requires_data_changes: bool
    # URLs or host names that appear in the instruction text.
    mentioned_targets: list[str]
    # Text in the input that tried to give orders rather than describe a test.
    ignored_instructions: list[str]

    @model_validator(mode="after")
    def _check(self) -> Self:
        _check_sizes(self.model_dump())

        objective_ids = [objective.id for objective in self.test_objectives]
        question_ids = [question.id for question in self.clarification_questions]
        for kind, ids in (("objective", objective_ids), ("question", question_ids)):
            if len(set(ids)) != len(ids):
                raise ValueError(f"{kind} ids must be unique")
            if not all(_ID.fullmatch(value) for value in ids):
                raise ValueError(f"{kind} ids must be short identifiers such as OBJ-1")

        covered = [behavior.objective_id for behavior in self.expected_behavior]
        if set(covered) - set(objective_ids):
            raise ValueError("expected_behavior refers to an objective that does not exist")
        if len(set(covered)) != len(covered):
            raise ValueError("at most one expected_behavior per objective")
        for behavior in self.expected_behavior:
            if behavior.source == "unknown" and behavior.description:
                raise ValueError(
                    "expected_behavior with source 'unknown' must have a null description: "
                    "do not guess the result"
                )
            if behavior.source != "unknown" and not behavior.description:
                raise ValueError(
                    "expected_behavior needs a description unless its source is unknown"
                )
        if self.is_feature_identifiable and not self.feature_name:
            raise ValueError("feature_name is required when the feature is identifiable")
        return self

    def behavior_for(self, objective_id: str) -> ExpectedBehavior | None:
        return next((b for b in self.expected_behavior if b.objective_id == objective_id), None)


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
