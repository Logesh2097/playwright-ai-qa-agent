"""Flaky test detection, from the history of a case's results. Pure and deterministic.

A test is flaky when the same version of it, in the same environment, gives
different answers. That is found in two ways:

- the engine or the workflow had to retry it and it then passed (status `flaky`);
- over several runs its outcome flips back and forth.

A test that passed for a while and then keeps failing is not flaky: something
changed. Results of different versions or environments are never compared.

Nothing here changes a result. A flaky test stays visible as flaky; it is not
quarantined, hidden or counted as passed.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise
from typing import Any

DECISIVE = frozenset({"passed", "failed", "error", "flaky", "needs_review"})
DEFAULT_WINDOW = 20
MIN_RUNS = 3
MIN_FLIPS = 2


@dataclass(frozen=True, slots=True)
class RunRecord:
    """One past result of a case."""

    finished_at: datetime
    status: str
    version_id: Any
    environment_id: Any
    failure_category: str | None = None
    failure_class: str | None = None
    healed: bool = False
    attempt_count: int = 1


@dataclass(frozen=True, slots=True)
class Stability:
    runs: int
    passed: int
    not_passed: int
    # Results that passed only after a retry.
    passed_on_retry: int
    # How often the outcome changed between consecutive runs of the same version and environment.
    flips: int
    is_flaky: bool
    # Why it looks flaky, most frequent first.
    kinds: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    last_status: str | None = None

    @property
    def flake_rate(self) -> float:
        return round(self.not_passed / self.runs, 2) if self.runs else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "runs": self.runs,
            "passed": self.passed,
            "not_passed": self.not_passed,
            "passed_on_retry": self.passed_on_retry,
            "flips": self.flips,
            "flake_rate": self.flake_rate,
            "is_flaky": self.is_flaky,
            "kinds": self.kinds,
            "evidence": self.evidence,
            "last_status": self.last_status,
        }


def _good(record: RunRecord) -> bool:
    return record.status == "passed"


def _kind(record: RunRecord) -> str:
    """What sort of instability one non-passing (or retried, or healed) result shows."""
    if record.healed or record.failure_class == "locator":
        return "unstable_locator"
    if record.failure_class == "timeout":
        return "timing"
    if record.failure_category == "network_failure" or record.failure_class in (
        "network",
        "navigation",
    ):
        return "network"
    if record.failure_category in ("browser_infrastructure_failure", "authentication_failure") or (
        record.failure_class in ("infrastructure", "environment")
    ):
        return "environment"
    return "intermittent"


def analyse(records: Sequence[RunRecord], *, window: int = DEFAULT_WINDOW) -> Stability:
    """Judge the stability of one case from its most recent results."""
    recent = sorted((r for r in records if r.status in DECISIVE), key=lambda r: r.finished_at)[
        -window:
    ]
    if not recent:
        return Stability(0, 0, 0, 0, 0, False)

    groups: dict[tuple[Any, Any], list[RunRecord]] = {}
    for record in recent:
        groups.setdefault((record.version_id, record.environment_id), []).append(record)

    flips = 0
    unstable: list[RunRecord] = []
    for group in groups.values():
        changes = sum(1 for a, b in pairwise(group) if _good(a) != _good(b))
        flips += changes
        if len(group) >= MIN_RUNS and changes >= MIN_FLIPS:
            # Went wrong and came right again without the test or the environment changing.
            unstable.extend(r for r in group if not _good(r))
    retried = [r for r in recent if r.status == "flaky"]
    healed = [r for r in recent if r.healed]
    signals = [*unstable, *retried, *(r for r in healed if r not in unstable)]

    kinds = [kind for kind, _ in Counter(_kind(r) for r in signals).most_common()]
    evidence: list[str] = []
    if retried:
        evidence.append(f"{len(retried)} of the last {len(recent)} runs passed only after a retry.")
    if unstable:
        evidence.append(
            f"The outcome changed {flips} times across the last {len(recent)} runs of the same version "
            "in the same environment."
        )
    if healed:
        evidence.append(f"{len(healed)} runs needed a replacement locator.")
    return Stability(
        runs=len(recent),
        passed=sum(1 for r in recent if _good(r)),
        not_passed=sum(1 for r in recent if not _good(r)),
        passed_on_retry=len(retried),
        flips=flips,
        is_flaky=bool(signals),
        kinds=kinds,
        evidence=evidence,
        last_status=recent[-1].status,
    )
