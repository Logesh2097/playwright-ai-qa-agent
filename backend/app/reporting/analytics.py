"""History, comparison, trends and dashboard figures. Pure functions over plain records.

The definitions used everywhere:

- a case **ran to an end** if its result is passed, failed, error, flaky or
  needs_review. Skipped and blocked cases did not run and are in no rate;
- **pass rate** = passed / ran to an end. Flaky and needs-review results are
  not passes;
- **failure rate** = (failed + error) / ran to an end;
- an execution **passed** only with verdict `passed`.

"Coverage" here means how much of the project's test cases is automated, run
and passing. It says nothing about which lines of the application's code ran.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.testing.flakiness import RunRecord, analyse

RAN = ("passed", "failed", "error", "flaky", "needs_review")
NOT_PASSED = ("failed", "error")
STATUSES = ("passed", "failed", "error", "flaky", "needs_review", "skipped", "blocked")
VERDICTS = ("passed", "failed", "error", "flaky", "needs_review", "inconclusive")
COVERAGE_NOTE = (
    "Test coverage indicators describe the project's test cases (automated, run, passing). "
    "They are not code coverage: no code instrumentation is integrated."
)


@dataclass(frozen=True, slots=True)
class ExecutionRow:
    id: Any
    status: str
    verdict: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    environment_id: Any = None
    suite_id: Any = None

    @property
    def duration_ms(self) -> int | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return max(0, int((self.finished_at - self.started_at).total_seconds() * 1000))


@dataclass(frozen=True, slots=True)
class ResultRow:
    id: Any
    execution_id: Any
    case_id: Any
    status: str
    finished_at: datetime | None
    failure_category: str | None = None
    failure_class: str | None = None
    failure_summary: str | None = None
    duration_ms: int | None = None
    version_id: Any = None
    environment_id: Any = None
    healed: bool = False
    attempt_count: int = 1


def rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def result_rates(statuses: Sequence[str]) -> dict[str, Any]:
    counts = Counter(statuses)
    ran = sum(counts[status] for status in RAN)
    return {
        "ran": ran,
        **{status: counts[status] for status in STATUSES},
        "pass_rate": rate(counts["passed"], ran),
        "failure_rate": rate(counts["failed"] + counts["error"], ran),
    }


# --- comparison ---------------------------------------------------------------------------


def change_of(base: str | None, target: str | None) -> str:
    """How one case's outcome differs between two executions."""
    if base is None:
        return "added"
    if target is None:
        return "removed"
    if base == target:
        return "still_failing" if base in NOT_PASSED else "unchanged"
    if target in NOT_PASSED:
        # From anything that was not a failure to a failure.
        return "still_failing" if base in NOT_PASSED else "regressed"
    if base in NOT_PASSED and target == "passed":
        return "fixed"
    return "changed"


def compare(
    base: Sequence[ResultRow], target: Sequence[ResultRow]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Per case: status in each execution and what changed. Then counts of each change."""
    before = {row.case_id: row for row in base}
    after = {row.case_id: row for row in target}
    rows: list[dict[str, Any]] = []
    for case_id in dict.fromkeys([*before, *after]):
        old, new = before.get(case_id), after.get(case_id)
        rows.append(
            {
                "case_id": case_id,
                "base_status": old.status if old else None,
                "target_status": new.status if new else None,
                "base_category": old.failure_category if old else None,
                "target_category": new.failure_category if new else None,
                "base_duration_ms": old.duration_ms if old else None,
                "target_duration_ms": new.duration_ms if new else None,
                "change": change_of(old.status if old else None, new.status if new else None),
            }
        )
    order = {"regressed": 0, "still_failing": 1, "fixed": 2, "changed": 3, "added": 4, "removed": 5}
    rows.sort(key=lambda row: order.get(row["change"], 6))
    counts = Counter(row["change"] for row in rows)
    names = ("regressed", "fixed", "still_failing", "changed", "added", "removed", "unchanged")
    return rows, {name: counts[name] for name in names}


# --- trends -------------------------------------------------------------------------------


def trend(
    executions: Sequence[ExecutionRow],
    results: Sequence[ResultRow],
    *,
    days: int,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """One entry per UTC day, oldest first, days without executions included."""
    last = today or datetime.now(UTC).date()
    first = last - timedelta(days=days - 1)
    day_of_execution: dict[Any, date] = {}
    by_day: dict[date, list[ExecutionRow]] = {}
    for execution in executions:
        moment = execution.finished_at or execution.created_at
        day = moment.astimezone(UTC).date()
        if first <= day <= last:
            by_day.setdefault(day, []).append(execution)
            day_of_execution[execution.id] = day
    statuses: dict[date, list[str]] = {}
    for result in results:
        found = day_of_execution.get(result.execution_id)
        if found is not None:
            statuses.setdefault(found, []).append(result.status)

    points: list[dict[str, Any]] = []
    for offset in range(days):
        day = first + timedelta(days=offset)
        ran = by_day.get(day, [])
        verdicts = Counter(e.verdict for e in ran if e.verdict)
        durations = [e.duration_ms for e in ran if e.duration_ms is not None]
        points.append(
            {
                "date": day.isoformat(),
                "executions": len(ran),
                "verdicts": {name: verdicts[name] for name in VERDICTS if verdicts[name]},
                "results": result_rates(statuses.get(day, [])),
                "average_duration_ms": int(sum(durations) / len(durations)) if durations else None,
            }
        )
    return points


# --- dashboard ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaseFacts:
    """What the dashboard needs to know about one test case."""

    id: Any
    key: str
    title: str
    status: str
    approved: bool
    automated: bool = field(default=False)


def stability_of(rows: Sequence[ResultRow], window: int = 20) -> Any:
    return analyse(
        [
            RunRecord(
                finished_at=row.finished_at,
                status=row.status,
                version_id=row.version_id,
                environment_id=row.environment_id,
                failure_category=row.failure_category,
                failure_class=row.failure_class,
                healed=row.healed,
                attempt_count=row.attempt_count,
            )
            for row in rows
            if row.finished_at is not None
        ],
        window=window,
    )


def frequently_failing(
    results: Sequence[ResultRow], cases: Mapping[Any, CaseFacts], *, limit: int = 10
) -> list[dict[str, Any]]:
    """Cases with the most failed or errored results, oldest-to-newest order assumed."""
    by_case: dict[Any, list[ResultRow]] = {}
    for row in results:
        if row.status in RAN:
            by_case.setdefault(row.case_id, []).append(row)
    found: list[dict[str, Any]] = []
    for case_id, rows in by_case.items():
        bad = [row for row in rows if row.status in NOT_PASSED]
        if not bad:
            continue
        case = cases.get(case_id)
        categories = Counter(row.failure_category for row in bad if row.failure_category)
        found.append(
            {
                "case_id": case_id,
                "case_key": case.key if case else None,
                "title": case.title if case else None,
                "runs": len(rows),
                "failures": len(bad),
                "failure_rate": rate(len(bad), len(rows)),
                "last_status": rows[-1].status,
                "last_failed_at": bad[-1].finished_at,
                "most_common_category": categories.most_common(1)[0][0] if categories else None,
            }
        )
    found.sort(
        key=lambda item: (-item["failures"], -(item["failure_rate"] or 0), str(item["case_key"]))
    )
    return found[:limit]


def coverage(cases: Sequence[CaseFacts], results: Sequence[ResultRow]) -> dict[str, Any]:
    """How much of the project's test cases is approved, automated, run and passing."""
    active = [case for case in cases if case.status != "archived"]
    approved = [case for case in active if case.approved]
    automated = {case.id for case in approved if case.automated}
    latest: dict[Any, str] = {}
    for row in results:  # oldest first: the last one wins
        if row.status in RAN and row.case_id in automated:
            latest[row.case_id] = row.status
    passing = sum(1 for status in latest.values() if status == "passed")
    return {
        "test_cases": len(active),
        "approved": len(approved),
        "automated": len(automated),
        "executed": len(latest),
        "passing": passing,
        "approved_share": rate(len(approved), len(active)),
        "automated_share": rate(len(automated), len(approved)),
        "executed_share": rate(len(latest), len(automated)),
        "passing_share": rate(passing, len(automated)),
        "note": COVERAGE_NOTE,
    }


def dashboard(
    executions: Sequence[ExecutionRow],
    results: Sequence[ResultRow],
    cases: Sequence[CaseFacts],
    *,
    days: int,
) -> dict[str, Any]:
    """`results` oldest first. Everything is for the window the rows were loaded for."""
    finished = [e for e in executions if e.verdict is not None]
    verdicts = Counter(e.verdict for e in finished)
    durations = [e.duration_ms for e in finished if e.duration_ms is not None]
    by_id = {case.id: case for case in cases}
    by_case: dict[Any, list[ResultRow]] = {}
    for row in results:
        by_case.setdefault(row.case_id, []).append(row)
    flaky = [case_id for case_id, rows in by_case.items() if stability_of(rows).is_flaky]
    return {
        "window_days": days,
        "total_executions": len(executions),
        "finished_executions": len(finished),
        "executions_by_verdict": {name: verdicts[name] for name in VERDICTS},
        "execution_pass_rate": rate(verdicts["passed"], len(finished)),
        "execution_failure_rate": rate(verdicts["failed"] + verdicts["error"], len(finished)),
        "results": result_rates([row.status for row in results]),
        "flaky_test_count": len(flaky),
        "flaky_case_ids": flaky[:50],
        "average_execution_duration_ms": int(sum(durations) / len(durations))
        if durations
        else None,
        "frequently_failing": frequently_failing(results, by_id),
        "coverage": coverage(cases, results),
    }
