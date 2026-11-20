"""Deterministic checks on a test case.

Applied identically to cases written by the model and by people, at generation,
on every edit, and again at approval. A case with a `blocking` issue cannot be
approved. Nothing here calls the model.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from app.agent.guard import mentioned_targets
from app.core.security.url_safety import Origin, UrlNotAllowedError, parse_origin
from app.testing.cases import (
    ACTION_PERMISSION,
    ASSERTION_ACTIONS,
    PLACEHOLDER,
    TARGETED_ACTIONS,
    VALUE_ACTIONS,
    CaseContent,
)

Severity = Literal["blocking", "warning"]

_READ_ONLY_SIDE_EFFECTS = frozenset({"none"})
_SENSITIVE_TARGET = re.compile(
    r"pass(word|code|phrase)?|secret|token|api[\s_-]?key|otp|pin\b|cvv|card\s*number", re.IGNORECASE
)
_VAGUE = frozenset(
    {"", "do it", "test", "test it", "check", "verify", "step", "n a", "tbd", "todo"}
)
_PLACEHOLDER_VALUE = re.compile(r"\bto\s+be\s+defined\b|\btbd\b", re.IGNORECASE)
_API_WRITE = re.compile(r"^\s*(post|put|patch|delete)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CaseIssue:
    code: str
    severity: Severity
    message: str
    # 1-based step number, where the issue is about one step.
    step: int | None = None


@dataclass(frozen=True, slots=True)
class CaseFacts:
    """What the validator needs to know about the plan and the environment."""

    objective_ids: frozenset[str]
    credential_roles: frozenset[str]
    allowed_origins: frozenset[Origin]
    environment_name: str
    environment_is_read_only: bool
    environment_allowed_actions: frozenset[str]
    # Normalised titles and step signatures of the other cases, for duplicate detection.
    other_titles: dict[str, str] = field(default_factory=dict)
    other_signatures: dict[tuple[object, ...], str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CaseReport:
    issues: list[CaseIssue]

    @property
    def ready(self) -> bool:
        return not any(issue.severity == "blocking" for issue in self.issues)

    def as_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "issues": [
                {"code": i.code, "severity": i.severity, "message": i.message, "step": i.step}
                for i in self.issues
            ],
        }


def _normal(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def validate_case(case: CaseContent, facts: CaseFacts) -> CaseReport:
    issues: list[CaseIssue] = []

    def block(code: str, message: str, step: int | None = None) -> None:
        issues.append(CaseIssue(code, "blocking", message, step))

    def warn(code: str, message: str, step: int | None = None) -> None:
        issues.append(CaseIssue(code, "warning", message, step))

    # 1. Meaningful steps.
    actions = [step for step in case.steps if step.action not in ASSERTION_ACTIONS]
    assertions = [step for step in case.steps if step.action in ASSERTION_ACTIONS]
    if not actions:
        block("no_steps", "The case has no execution steps.")
    if not assertions and case.automation_feasibility != "manual_only":
        block(
            "no_verification_step",
            "No step checks the outcome. Add an assertion, or mark the case as manual only.",
        )
    for number, step in enumerate(case.steps, start=1):
        if _normal(step.description) in _VAGUE or len(step.description) < 5:
            block("vague_step", "The step does not say what to do.", number)
        if step.action in TARGETED_ACTIONS and not step.target:
            block("step_without_target", f"A '{step.action}' step must name the element.", number)
        if step.action in VALUE_ACTIONS and not step.value:
            block("step_without_value", f"A '{step.action}' step needs a value.", number)
        if _PLACEHOLDER_VALUE.search(step.value or ""):
            # Left by the generator where the compared value was unknown. Supplying the
            # expected result is not enough: the step that checks it must say what to check.
            block(
                "placeholder_in_step",
                "This step still says TO BE DEFINED. Replace it with the value to check.",
                number,
            )

    # 2. An expected result, from a real source.
    if case.expected_source == "unknown" or not case.expected_result:
        block(
            "missing_expected_result",
            "The expected result is not known. It has to come from the requirement, a configured "
            "rule or a person; it is not guessed.",
        )
    elif len(_normal(case.expected_result)) < 8:
        block("vague_expected_result", "The expected result is too vague to verify.")
    if _normal(case.verification.description) in _VAGUE:
        warn(
            "vague_verification", "The verification strategy does not say how a pass is recognised."
        )

    # 3. Test data: defined, complete, and free of secrets.
    defined = {item.name for item in case.test_data}
    used: set[str] = set()
    for number, step in enumerate(case.steps, start=1):
        for kind, name in PLACEHOLDER.findall(step.value or ""):
            if kind == "data":
                used.add(name)
                if name not in defined:
                    block(
                        "undefined_test_data",
                        f"The step uses {{{{data.{name}}}}}, which is not in the test data.",
                        number,
                    )
            else:
                role = name.split(".", 1)[0]
                if role.lower() not in {r.lower() for r in facts.credential_roles}:
                    available = ", ".join(sorted(facts.credential_roles)) or "none"
                    block(
                        "credential_missing",
                        f"The step needs a test account with the role '{role}', but the environment "
                        f"'{facts.environment_name}' has none (available: {available}).",
                        number,
                    )
        # A secret typed as a literal would be stored and shown in clear.
        typed_literal = step.action == "fill" and step.value and not PLACEHOLDER.search(step.value)
        # Deliberately wrong or empty passwords are legitimate negative-test input.
        deliberate = case.category in ("negative", "form_validation", "boundary", "edge_case")
        sensitive = _SENSITIVE_TARGET.search(f"{step.target or ''} {step.description}")
        if typed_literal and sensitive and not deliberate:
            block(
                "literal_secret_in_step",
                "This step types a secret as plain text. Use a {{cred.ROLE.FIELD}} placeholder.",
                number,
            )
    for item in case.test_data:
        if item.value is None and item.name in used:
            block("test_data_value_missing", f"The test data item '{item.name}' has no value yet.")
        elif item.name not in used:
            warn("unused_test_data", f"The test data item '{item.name}' is not used by any step.")

    # 4. Duplicates.
    title_owner = facts.other_titles.get(case.title_key())
    signature_owner = facts.other_signatures.get(case.signature()) if case.steps else None
    if signature_owner or title_owner:
        warn(
            "possible_duplicate",
            f"This looks like the same scenario as {signature_owner or title_owner}.",
        )

    # 5. Relevance to the requirement.
    if case.objective_id is None:
        warn(
            "not_in_requirement",
            "This scenario goes beyond the approved requirement. Keep it only if it is wanted.",
        )
    elif case.objective_id not in facts.objective_ids:
        block(
            "unknown_objective",
            f"The case refers to objective '{case.objective_id}', which the plan does not contain.",
        )

    # 6. Unsupported assumptions.
    if case.assumptions:
        block(
            "unsupported_assumptions",
            "The case rests on assumptions nobody confirmed: "
            + "; ".join(case.assumptions[:3])
            + ". Confirm them (move them to the preconditions) or remove them.",
        )

    # 7. Scope and what the environment permits.
    for number, step in enumerate(case.steps, start=1):
        for url in _urls(step):
            try:
                origin = parse_origin(url)
            except UrlNotAllowedError:
                block("invalid_url", f"'{url}' is not a valid http(s) URL.", number)
                continue
            if origin not in facts.allowed_origins:
                block(
                    "out_of_scope_url",
                    f"{origin} is not one of the allowed origins of '{facts.environment_name}'.",
                    number,
                )
        needed = _permission(step.action, step.value)
        if needed not in facts.environment_allowed_actions:
            block(
                "action_not_allowed",
                f"The environment '{facts.environment_name}' does not permit '{needed}' actions.",
                number,
            )
        if facts.environment_is_read_only and step.side_effect not in _READ_ONLY_SIDE_EFFECTS:
            block(
                "mutating_step_on_read_only",
                f"This step would {step.side_effect} data, but '{facts.environment_name}' is read-only.",
                number,
            )
    return CaseReport(issues)


def _permission(action: str, value: str | None) -> str:
    if action in ASSERTION_ACTIONS:
        return "read"
    if action == "api_request" and _API_WRITE.match(value or ""):
        return "api_write"
    return ACTION_PERMISSION[action]


def _urls(step: object) -> list[str]:
    """Absolute addresses a step would contact. Relative paths stay inside the environment."""
    action, value = step.action, step.value or ""  # type: ignore[attr-defined]
    found = [t.text for t in mentioned_targets(value) if t.explicit]
    if action == "navigate" and "://" in value and value not in found:
        found.append(value)
    return [url if "://" in url else f"http://{url}" for url in found]


def index_cases(
    cases: Iterable[tuple[str, CaseContent]],
) -> tuple[dict[str, str], dict[tuple[object, ...], str]]:
    """Lookup tables for duplicate detection: normalised title / step signature -> case label."""
    titles: dict[str, str] = {}
    signatures: dict[tuple[object, ...], str] = {}
    for label, content in cases:
        titles.setdefault(content.title_key(), label)
        if content.steps:
            signatures.setdefault(content.signature(), label)
    return titles, signatures


def remove_duplicates[T: CaseContent](cases: Sequence[T]) -> tuple[list[T], int]:
    """Keep the first of each scenario. Returns the survivors and how many were dropped."""
    seen_titles: set[str] = set()
    seen_signatures: set[tuple[object, ...]] = set()
    kept: list[T] = []
    for case in cases:
        signature = case.signature() if case.steps else None
        if case.title_key() in seen_titles or (signature and signature in seen_signatures):
            continue
        seen_titles.add(case.title_key())
        if signature:
            seen_signatures.add(signature)
        kept.append(case)
    return kept, len(cases) - len(kept)
