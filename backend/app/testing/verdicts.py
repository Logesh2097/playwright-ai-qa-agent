"""The verdict of an execution, from the statuses of its results. Pure and deterministic.

No model output is read here or anywhere on the way to a verdict.
"""

from collections import Counter
from collections.abc import Iterable

# Results that say nothing about the application because the case did not run to an end.
NOT_EXECUTED = frozenset({"pending", "running", "skipped", "blocked", "inconclusive"})


def execution_verdict(statuses: Iterable[str]) -> str:
    """
    failed        at least one case found the application doing the wrong thing
    error         none failed, but at least one could not be carried out
    inconclusive  none failed or errored, but not every case ran (or there were none)
    needs_review  all ran and none failed, but at least one result has to be looked at
                  by a person (for example it only passed with a replacement locator)
    flaky         all ran and passed, at least one only after a retry
    passed        every case ran and passed first time, as written
    """
    counts = Counter(statuses)
    if counts["failed"]:
        return "failed"
    if counts["error"]:
        return "error"
    if not counts or any(counts[status] for status in NOT_EXECUTED):
        return "inconclusive"
    if counts["needs_review"]:
        return "needs_review"
    if counts["flaky"]:
        return "flaky"
    return "passed"


def totals(statuses: Iterable[str]) -> dict[str, int]:
    counts = Counter(statuses)
    names = ("passed", "failed", "error", "flaky", "skipped", "blocked", "needs_review")
    return {"total": sum(counts.values()), **{name: counts[name] for name in names}}
