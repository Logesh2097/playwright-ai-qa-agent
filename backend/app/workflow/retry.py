"""Which results may be run again. Decided from recorded facts, never by a model.

Only failures that say nothing about the application are retried: the browser
or engine broke, or the network did. An assertion that did not hold is a
finding and is not retried, and nothing is retried if the first attempt may
already have changed data in the application.
"""

from dataclasses import dataclass

# Step error types that can go away by themselves.
TRANSIENT_ERROR_TYPES = frozenset({"internal", "request_failed", "navigation"})
# Reasons an execution as a whole could not use the engine or the browser.
TRANSIENT_EXECUTION_ERRORS = frozenset(
    {
        "engine_unavailable",
        "engine_busy",
        "engine_protocol",
        "runner_unavailable",
        "internal",
        "worker_interrupted",
    }
)


@dataclass(frozen=True, slots=True)
class Eligibility:
    eligible: bool
    reason: str


def retry_eligibility(
    *,
    status: str,
    failure_class: str | None,
    error_type: str | None,
    changed_state: bool,
    execution_error: str | None,
) -> Eligibility:
    if status in ("passed", "flaky"):
        return Eligibility(False, "The case passed.")
    if status == "failed":
        return Eligibility(
            False, "An assertion did not hold. That is a finding, not a fault to retry."
        )
    if status in ("blocked", "inconclusive", "needs_review"):
        return Eligibility(False, "The case cannot run as it is; running it again would not help.")
    if changed_state:
        return Eligibility(
            False, "The first attempt may already have changed data, so it is not repeated."
        )
    if status == "skipped":
        if execution_error in TRANSIENT_EXECUTION_ERRORS:
            return Eligibility(True, "The engine could not be used; the case never started.")
        return Eligibility(False, "The case was not run.")
    if failure_class == "infrastructure" or error_type in TRANSIENT_ERROR_TYPES:
        return Eligibility(True, "The browser, the engine or the network failed, not the test.")
    return Eligibility(False, "The failure is not of a kind that goes away by itself.")
