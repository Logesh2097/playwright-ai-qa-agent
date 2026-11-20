"""Deterministic checks on a requirement analysis.

The LLM proposes; this module decides whether the requirement is ready for
planning. Every check is plain code over the analysis and the platform's own
records, so the verdict does not depend on the model agreeing.

A `blocking` issue means the requirement cannot be approved. Blocking issues
that a person can resolve by answering become clarification questions; the
others (a missing credential, an archived application) say what to change.
"""

from dataclasses import dataclass, field
from typing import Literal

from app.agent.guard import mentioned_targets, suspicious_phrases
from app.agent.schemas.requirement import ClarificationQuestion, RequirementAnalysis
from app.core.security.url_safety import (
    UrlNotAllowedError,
    host_in_domains,
    normalize_host,
    parse_origin,
)

Severity = Literal["blocking", "warning"]


@dataclass(frozen=True, slots=True)
class Issue:
    code: str
    severity: Severity
    message: str
    # What the issue is about: an objective id, a data item name, ...
    subject: str | None = None


@dataclass(frozen=True, slots=True)
class TargetFacts:
    """What the validator needs to know about the application and environment."""

    application_name: str
    application_status: str
    application_auth_method: str
    allowed_domains: list[str]
    environment_name: str
    environment_status: str
    environment_is_read_only: bool
    environment_allowed_actions: list[str]
    credential_roles: list[str]
    variable_names: list[str]
    configured_rule_names: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ValidationReport:
    issues: list[Issue]
    questions: list[ClarificationQuestion]

    @property
    def ready(self) -> bool:
        """True when nothing blocks approval and nothing is left to ask."""
        return not self.questions and not any(i.severity == "blocking" for i in self.issues)


def _host_of(target: str) -> str | None:
    candidate = target if "://" in target else f"https://{target}"
    try:
        return parse_origin(candidate).host
    except UrlNotAllowedError:
        try:
            return normalize_host(target.split("/")[0].split(":")[0])
        except UrlNotAllowedError:
            return None


def validate_requirement(
    analysis: RequirementAnalysis,
    facts: TargetFacts,
    *,
    instruction_text: str,
    answered_question_ids: set[str],
) -> ValidationReport:
    issues: list[Issue] = []
    generated: list[ClarificationQuestion] = []

    def block(code: str, message: str, subject: str | None = None) -> None:
        issues.append(Issue(code, "blocking", message, subject))

    def warn(code: str, message: str, subject: str | None = None) -> None:
        issues.append(Issue(code, "warning", message, subject))

    # 1. Is the application configured and usable?
    if facts.application_status != "active":
        block(
            "application_not_active",
            f"The application '{facts.application_name}' is archived. Restore it before testing it.",
        )

    # 2. Does the target environment exist in a usable state?
    if facts.environment_status != "active":
        block(
            "environment_not_active",
            f"The environment '{facts.environment_name}' is disabled.",
        )

    # 3. Is the feature identifiable?
    if not analysis.is_feature_identifiable or not analysis.feature_name:
        block("feature_not_identified", "It is not clear which feature should be tested.")
        generated.append(
            ClarificationQuestion(
                id="AUTO-feature",
                question="Which feature or screen of the application should be tested?",
                options=[],
                related_to=None,
            )
        )
    if not analysis.test_objectives:
        block("no_objectives", "No concrete test objective could be derived from the instruction.")

    # 4. Is the expected behaviour clear for every objective? Never filled in by guessing.
    for objective in analysis.test_objectives:
        behavior = analysis.behavior_for(objective.id)
        if behavior is None or behavior.source == "unknown":
            block(
                "expected_behavior_unknown",
                f"The expected result is not stated for: {objective.title}",
                objective.id,
            )
            generated.append(
                ClarificationQuestion(
                    id=f"AUTO-{objective.id}",
                    question=f"What exactly should happen in this case: {objective.title}?",
                    options=[],
                    related_to=objective.id,
                )
            )

    # 5. Business rules that depend on a value nobody supplied.
    for rule in analysis.business_rules:
        if not rule.value_known or rule.source == "unknown":
            block(
                "business_rule_value_missing",
                f"The rule '{rule.name}' depends on a value that has not been provided.",
                rule.name,
            )
            generated.append(
                ClarificationQuestion(
                    id=f"AUTO-rule-{len(generated) + 1}",
                    question=f"What is the exact rule or value for '{rule.name}'? {rule.description}",
                    options=[],
                    related_to=rule.name,
                )
            )

    # 6. Do the required credentials exist? Is required test data available?
    known_roles = {role.lower() for role in facts.credential_roles}
    needs_login = False
    for item in analysis.required_test_data:
        if item.kind == "credential":
            needs_login = True
            role = (item.credential_role or "").lower()
            if not role or role not in known_roles:
                available = ", ".join(sorted(facts.credential_roles)) or "none"
                block(
                    "credential_missing",
                    f"A test account for the role '{item.credential_role or item.name}' is needed, "
                    f"but the environment '{facts.environment_name}' has no credential with that "
                    f"role (available: {available}). Add one to the environment.",
                    item.name,
                )
        elif item.kind == "configuration_value":
            if (
                not item.values
                and item.name not in facts.variable_names
                and item.source == "unknown"
            ):
                block(
                    "test_data_missing",
                    f"The value of '{item.name}' is needed and has not been provided.",
                    item.name,
                )
                generated.append(
                    ClarificationQuestion(
                        id=f"AUTO-data-{len(generated) + 1}",
                        question=f"What is the value of '{item.name}'? {item.description}",
                        options=[],
                        related_to=item.name,
                    )
                )
        elif item.kind == "input_value":
            if not item.values and item.source == "unknown":
                warn(
                    "test_data_unspecified",
                    f"No concrete values were given for '{item.name}'. Values will have to be "
                    "chosen when test cases are written.",
                    item.name,
                )
        elif item.kind == "existing_record":
            warn(
                "test_data_unverified",
                f"The test relies on existing data: {item.name}. The platform cannot confirm "
                "that it exists in the environment.",
                item.name,
            )
    if needs_login and not facts.credential_roles:
        warn(
            "no_credentials_configured",
            f"The environment '{facts.environment_name}' has no test credentials at all.",
        )

    # 7. Does the requirement fit what the environment permits?
    if analysis.requires_data_changes and facts.environment_is_read_only:
        block(
            "environment_read_only",
            f"These tests would change data, but the environment '{facts.environment_name}' is "
            "read-only. Choose another environment or narrow the instruction.",
        )

    # 8. Scope: the instruction must not point the platform at anything outside the application.
    targets = {t.text.lower(): t for t in mentioned_targets(instruction_text)}
    for reported in analysis.mentioned_targets:
        for target in mentioned_targets(reported):
            targets.setdefault(target.text.lower(), target)
    for target in targets.values():
        host = _host_of(target.text)
        if host is None or host_in_domains(host, facts.allowed_domains):
            continue
        message = (
            f"The instruction mentions '{target.text}', which is outside the allowed domains of "
            f"'{facts.application_name}'. Tests can only run against the registered application."
        )
        if target.explicit:
            block("out_of_scope_target", message, target.text)
        else:
            warn("possible_out_of_scope_target", message, target.text)

    # 9. Signs that the text is addressed to the model rather than describing a test.
    reasons = suspicious_phrases(instruction_text)
    if reasons or analysis.ignored_instructions:
        detail = "; ".join(reasons) or "the analysis reported instructions it disregarded"
        warn(
            "possible_prompt_injection",
            f"Part of the instruction reads like an attempt to instruct the AI ({detail}). "
            "It was treated as plain text. Review the requirement carefully before approving.",
        )

    # Questions: the model's own plus the generated ones, minus those already answered.
    questions: dict[str, ClarificationQuestion] = {}
    related: set[str] = set()
    for question in analysis.clarification_questions:
        if question.id not in answered_question_ids:
            questions[question.id] = question
            if question.related_to:
                related.add(question.related_to)
    for question in generated:
        # Skip a generated question when the model already asks about the same thing.
        if question.id in answered_question_ids or question.related_to in related:
            continue
        questions.setdefault(question.id, question)

    # Information the model flagged as missing and blocking, but did not ask about.
    for missing in analysis.missing_information:
        if missing.blocking and not questions:
            block("missing_information", f"{missing.topic}: {missing.why_needed}", missing.topic)

    return ValidationReport(issues=issues, questions=list(questions.values()))
