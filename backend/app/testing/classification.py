"""What kind of problem a result that did not pass points at.

Decided by code, from what was recorded during the run. No model is asked, and
a model's later explanation never changes the category.

The point of the categories is to keep different things apart. A browser that
crashed, a network that dropped, a script that cannot find its button and a
login that was refused are not bugs in the application, and are not filed as
such. Only two things count as evidence about the application itself: an
assertion about it that did not hold, and the application visibly breaking
(a server error, an uncaught script error) while the test ran.

    application_failure             the application itself errored during the test
    test_assertion_failure          what the test expects is not what the application did
    test_automation_failure         the test could not do its work: element not found or
                                    ambiguous, missing test data, a step not allowed
    browser_infrastructure_failure  the browser, the engine or the worker failed
    network_failure                 a page or request could not be reached
    authentication_failure          signing in with the test account failed
    inconclusive                    nothing reliable can be said: it did not run, ran out
                                    of time, or its record does not support its status
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

_AUTOMATION = frozenset(
    {
        "locator_not_found",
        "locator_ambiguous",
        "test_data",
        "action_failed",
        "action_not_allowed",
        "environment_read_only",
        "destination_not_allowed",
    }
)
_INFRASTRUCTURE = frozenset({"internal"})
_NETWORK = frozenset({"navigation", "request_failed"})
_TIMING = frozenset({"timeout", "test_timeout"})
_ENGINE_ERRORS = frozenset(
    {
        "engine_unavailable",
        "engine_busy",
        "engine_protocol",
        "engine_rejected_plan",
        "engine_not_configured",
        "runner_unavailable",
        "internal",
        "internal_error",
        "worker_interrupted",
        "run_timeout",
    }
)


@dataclass(frozen=True, slots=True)
class Classification:
    category: str
    # The recorded facts the category rests on, in plain words.
    basis: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"category": self.category, "basis": self.basis, "decided_by": "rules"}


def application_evidence(
    details: Mapping[str, Any], actual: Mapping[str, Any] | None = None
) -> list[str]:
    """Signs that the application itself broke while the case ran."""
    evidence: list[str] = []
    for request in details.get("failed_requests", []) or []:
        status = request.get("status") if isinstance(request, Mapping) else None
        if isinstance(status, int) and status >= 500:
            evidence.append(
                f"The application answered {status} to {request.get('method', 'a request')} {request.get('url', '')}".strip()
            )
    for error in details.get("page_errors", []) or []:
        evidence.append(f"Uncaught error in the page: {str(error)[:160]}")
    status = (actual or {}).get("status")
    if isinstance(status, int) and status >= 500:
        evidence.append(f"The API answered {status}")
    return evidence[:5]


def classify(
    *,
    status: str,
    error_type: str | None,
    failure_class: str | None,
    details: Mapping[str, Any] | None = None,
    actual: Mapping[str, Any] | None = None,
    execution_error: str | None = None,
) -> Classification | None:
    """The category of one result, or None for a result that passed."""
    details = details or {}
    if status in ("passed", "flaky"):
        return None
    if status in ("pending", "running"):
        return None
    if status == "needs_review":
        return Classification(
            "inconclusive", ["The result needs a person's review before it counts."]
        )
    if status == "blocked":
        return Classification(
            "inconclusive", ["The case could not be run in this environment as written."]
        )
    if status == "skipped":
        if execution_error in _ENGINE_ERRORS:
            return Classification(
                "browser_infrastructure_failure",
                ["The engine could not be used, so the case never started."],
            )
        return Classification("inconclusive", ["The case was not run."])

    broke = application_evidence(details, actual)
    if status == "failed":
        if broke:
            return Classification("application_failure", ["An assertion did not hold.", *broke])
        return Classification(
            "test_assertion_failure",
            [
                "An assertion did not hold: the application did something other than the case expects."
            ],
        )

    # status == "error" (or anything unexpected): a step could not be carried out.
    if error_type == "login_failed":
        return Classification(
            "authentication_failure", ["Signing in with the test account did not succeed."]
        )
    if (
        error_type in _INFRASTRUCTURE
        or failure_class == "infrastructure"
        or execution_error in _ENGINE_ERRORS
    ):
        return Classification(
            "browser_infrastructure_failure", ["The browser, the engine or the worker failed."]
        )
    if error_type in _NETWORK:
        return Classification(
            "network_failure", ["A page or request could not be reached.", *broke]
        )
    if error_type in _AUTOMATION:
        if broke and error_type in ("locator_not_found", "action_failed"):
            # The element may be missing because the page itself failed.
            return Classification(
                "application_failure", ["A step could not be carried out.", *broke]
            )
        return Classification(
            "test_automation_failure",
            [_AUTOMATION_REASON.get(error_type, "The test could not carry out a step.")],
        )
    if error_type in _TIMING:
        if broke:
            return Classification("application_failure", ["A step ran out of time.", *broke])
        return Classification(
            "inconclusive",
            [
                "A step ran out of time. That alone does not say whether the application or the test is at fault."
            ],
        )
    return Classification("inconclusive", ["The recorded error is not of a known kind."])


_AUTOMATION_REASON = {
    "locator_not_found": "The script's locator matched no element.",
    "locator_ambiguous": "The script's locator matched several elements.",
    "test_data": "A value the script needs was missing or unusable.",
    "action_failed": "The element could not be used the way the step requires.",
    "action_not_allowed": "The environment does not allow this kind of step.",
    "environment_read_only": "The step would change data on a read-only environment.",
    "destination_not_allowed": "The script tried to reach an address outside the environment's allowed origins.",
}


def verification_summary(steps: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """What a result's verdict rests on: how many checks of which kind ran and held."""
    kinds = {"ui": 0, "api": 0, "business_rule": 0}
    held = failed = 0
    for step in steps:
        action = str(step.get("action", ""))
        if not action.startswith("assert_") or step.get("status") == "skipped":
            continue
        kind = (
            "business_rule"
            if action == "assert_expression"
            else "api"
            if action.startswith("assert_api")
            else "ui"
        )
        kinds[kind] += 1
        if step.get("status") == "passed":
            held += 1
        elif step.get("status") == "failed":
            failed += 1
    return {
        "assertions_evaluated": held + failed,
        "assertions_held": held,
        "assertions_failed": failed,
        "by_kind": {kind: count for kind, count in kinds.items() if count},
        "decided_by": "engine assertions",
    }
