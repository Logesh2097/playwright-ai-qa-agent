"""Failure reports: the recorded facts about a result that did not pass, and a model's reading of them.

Two things are kept apart throughout:

- the **record**: what the engine observed, and the category the rules in
  `app.testing.classification` gave it. This is what the result is;
- the **suggestion**: a model's summary, possible cause, impact and next steps.
  It is stored next to the result, labelled as a suggestion, and nothing reads
  it to decide anything.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from app.agent.nodes.analyse_failures import PROMPT_NAME
from app.agent.prompts import get_prompt
from app.agent.schemas.failure import FailureExplanation
from app.models import TestEvidence, TestExecutionStep, TestResult
from app.testing.classification import classify

SUGGESTION_LABEL = (
    "AI-generated suggestion. Not a confirmed root cause: it has not been checked "
    "against the application's code or data, and it does not change the result."
)
EXPLAINABLE = frozenset({"failed", "error"})
MAX_LOG_STEPS = 12
MAX_DEBUG_STEPS = 5
MAX_SUGGESTED_CASES = 3

# Which rule-decided categories each of the model's causes is compatible with.
_COMPATIBLE: dict[str, frozenset[str]] = {
    "application_defect": frozenset({"application_failure", "test_assertion_failure"}),
    "test_script": frozenset({"test_automation_failure", "test_assertion_failure"}),
    "test_data": frozenset({"test_automation_failure"}),
    "environment": frozenset(
        {
            "browser_infrastructure_failure",
            "network_failure",
            "authentication_failure",
            "test_automation_failure",
            "inconclusive",
        }
    ),
}


def failed_step(steps: Sequence[TestExecutionStep]) -> TestExecutionStep | None:
    """The step that stopped the last attempt (cleanup problems do not decide a result)."""
    last = max((step.attempt for step in steps), default=0)
    return next(
        (
            step
            for step in steps
            if step.attempt == last
            and step.phase != "cleanup"
            and step.status in ("failed", "error")
        ),
        None,
    )


def category_of(
    result: TestResult, steps: Sequence[TestExecutionStep], execution_error: str | None = None
) -> dict[str, Any] | None:
    """The stored classification, or the same rules applied again for older results."""
    stored = (result.details or {}).get("classification")
    if isinstance(stored, dict) and stored.get("category"):
        return stored
    bad = failed_step(steps)
    category = classify(
        status=result.status,
        error_type=bad.error_type if bad else None,
        failure_class=result.failure_class,
        details=result.details,
        actual=bad.actual if bad else None,
        execution_error=execution_error,
    )
    return category.as_dict() if category else None


def failure_record(
    result: TestResult,
    steps: Sequence[TestExecutionStep],
    evidence: Sequence[TestEvidence] = (),
    *,
    case_key: str,
    title: str = "",
    expected_result: str | None = None,
    execution_error: str | None = None,
) -> dict[str, Any]:
    """Everything recorded about why a result did not pass. Facts only."""
    bad = failed_step(steps)
    attempt = bad.attempt if bad else max((s.attempt for s in steps), default=0)
    details = result.details or {}
    log = [
        {
            "position": step.position,
            "phase": step.phase,
            "action": step.action,
            "description": step.description,
            "status": step.status,
            "duration_ms": step.duration_ms,
        }
        for step in steps
        if step.attempt == attempt and (bad is None or step.position <= bad.position)
    ][-MAX_LOG_STEPS:]
    shots = [item for item in evidence if item.result_id == result.id and item.kind == "screenshot"]
    at_failure = [item for item in shots if bad is not None and item.execution_step_id == bad.id]
    return {
        "result_id": str(result.id),
        "case": {"key": case_key, "title": title, "expected_result": expected_result},
        "status": result.status,
        "attempts": result.attempt_count,
        "failure_summary": result.failure_summary,
        "classification": category_of(result, steps, execution_error),
        "failed_step": (
            {
                "position": bad.position,
                "phase": bad.phase,
                "action": bad.action,
                "description": bad.description,
                "locator": bad.resolved_locator,
                "expected": bad.expected,
                "actual": bad.actual,
                "error_type": bad.error_type,
                "browser_error": bad.error_message,
                "page_url": bad.page_url,
            }
            if bad
            else None
        ),
        "console_errors": list(details.get("console_errors", []))[:10],
        "page_errors": list(details.get("page_errors", []))[:10],
        "network_errors": {
            "failed_requests": list(details.get("failed_requests", []))[:10],
            "blocked_requests": list(details.get("blocked_requests", []))[:10],
        },
        "screenshot_evidence_ids": [str(item.id) for item in (at_failure or shots)[:5]],
        "execution_log": log,
        "verification": details.get("verification"),
    }


def facts_for_model(ref: str, record: dict[str, Any]) -> dict[str, Any]:
    """The part of a record a model is given. No identifiers, no evidence files."""
    classification = record.get("classification") or {}
    return {
        "ref": ref,
        "case": record["case"],
        "status": record["status"],
        "category_by_rules": classification.get("category"),
        "step_that_did_not_pass": record["failed_step"],
        "steps_before": record["execution_log"][:-1] if record["failed_step"] else [],
        "console_errors": record["console_errors"][:5],
        "page_errors": record["page_errors"][:5],
        "failed_requests": record["network_errors"]["failed_requests"][:5],
        "blocked_requests": record["network_errors"]["blocked_requests"][:5],
    }


def as_suggestion(
    explanation: FailureExplanation, *, category: str | None, model: str
) -> dict[str, Any]:
    """A model's explanation in the form it is stored and shown: clearly a suggestion."""
    body = explanation.model_dump(exclude={"ref"})
    body["debugging_steps"] = body["debugging_steps"][:MAX_DEBUG_STEPS]
    body["suggested_test_cases"] = body["suggested_test_cases"][:MAX_SUGGESTED_CASES]
    compatible = _COMPATIBLE.get(explanation.likely_cause)
    return {
        **body,
        "advisory": True,
        "label": SUGGESTION_LABEL,
        # The category stays what the rules said. This only tells a reader whether
        # the model's reading fits it; where it does not, the rules stand.
        "category_by_rules": category,
        "agrees_with_rules": None
        if compatible is None or category is None
        else category in compatible,
        "model": model,
        "prompt": get_prompt(PROMPT_NAME).ref,
        "generated_at": datetime.now(UTC).isoformat(),
    }
