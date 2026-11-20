"""What a CI system is told about an execution: one outcome, an exit code, JUnit XML.

All of it is derived from the execution's verdict, which is derived from the
verified results. Nothing here looks at a model's output.

    outcome       exit   when
    passed        0      every case ran and passed first time, as written
    failed        1      at least one assertion did not hold
    unverified    2      nothing failed, but the run is not a verified pass: an error,
                         cases that did not run, a pass on retry (flaky), or a pass that
                         needs review
    incomplete    3      the execution itself did not finish (cancelled, timed out,
                         engine unavailable)
    (running)     -      not finished yet; no exit code

Only `passed` is success. A CI job that wants to tolerate flaky passes has to say
so itself (`--allow-flaky` in the CLI); the platform never decides that for it.
"""

import re
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree as ET

from app.reporting.model import ExecutionReport, ResultDetail

ACTIVE = frozenset(
    {"queued", "awaiting_approval", "preparing", "running", "analysing", "reporting", "cancelling"}
)
EXIT_PASSED, EXIT_FAILED, EXIT_UNVERIFIED, EXIT_INCOMPLETE = 0, 1, 2, 3
_ILLEGAL_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


@dataclass(frozen=True, slots=True)
class CiOutcome:
    finished: bool
    outcome: str  # running, passed, failed, unverified, incomplete
    passed: bool
    exit_code: int | None
    summary: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "finished": self.finished,
            "outcome": self.outcome,
            "passed": self.passed,
            "exit_code": self.exit_code,
            "summary": self.summary,
        }


_UNVERIFIED = {
    "error": "At least one test could not be carried out.",
    "inconclusive": "Not every test ran, so the result is not a verified pass.",
    "flaky": "All tests passed, at least one only after a retry.",
    "needs_review": "Nothing failed, but at least one result needs a person's review.",
}


def ci_outcome(status: str, verdict: str | None, totals: dict[str, Any] | None = None) -> CiOutcome:
    counts = totals or {}
    if status in ACTIVE or (verdict is None and status == "completed"):
        return CiOutcome(False, "running", False, None, "The execution has not finished.")
    if status != "completed":
        return CiOutcome(
            True, "incomplete", False, EXIT_INCOMPLETE, f"The execution did not finish ({status})."
        )
    if verdict == "passed":
        return CiOutcome(
            True, "passed", True, EXIT_PASSED, f"All {counts.get('total', 0)} test(s) passed."
        )
    if verdict == "failed":
        return CiOutcome(
            True, "failed", False, EXIT_FAILED,
            f"{counts.get('failed', 0)} of {counts.get('total', 0)} test(s) failed.",
        )  # fmt: skip
    return CiOutcome(
        True, "unverified", False, EXIT_UNVERIFIED,
        _UNVERIFIED.get(verdict or "", "The result is not a verified pass."),
    )  # fmt: skip


def xml_text(value: Any, limit: int = 8000) -> str:
    """Text an XML 1.0 document may contain."""
    text = _ILLEGAL_XML.sub(" ", "" if value is None else str(value))
    return text if len(text) <= limit else text[:limit] + "…"


def _failed_step(result: ResultDetail) -> Any:
    last = max((step.attempt for step in result.steps), default=0)
    return next(
        (
            step
            for step in result.steps
            if step.attempt == last
            and step.phase != "cleanup"
            and step.status in ("failed", "error")
        ),
        None,
    )


def to_junit(report: ExecutionReport) -> bytes:
    """The report as JUnit XML, the format CI systems read.

    JUnit has four outcomes. `failed` is a failure; `error` is an error; skipped
    and blocked are skipped. Flaky and needs-review results are reported as
    failures with their own type, because a reader of JUnit would otherwise
    count them as passes.
    """
    totals = report.totals
    failures = totals.failed + totals.flaky + totals.needs_review
    suite = ET.Element(
        "testsuite",
        {
            "name": xml_text(report.suite.name if report.suite else report.project.name, 200),
            "tests": str(totals.total),
            "failures": str(failures),
            "errors": str(totals.error),
            "skipped": str(totals.skipped + totals.blocked),
            "time": f"{(report.execution.duration_ms or 0) / 1000:.3f}",
            "timestamp": report.execution.started_at.isoformat()
            if report.execution.started_at
            else "",
            "id": str(report.execution.id),
        },
    )
    properties = ET.SubElement(suite, "properties")
    for name, value in (
        ("execution_id", report.execution.id),
        ("verdict", report.execution.verdict or ""),
        ("project", report.project.name),
        ("environment", report.environment.name if report.environment else ""),
        ("browser", report.execution.browser or ""),
    ):
        ET.SubElement(properties, "property", {"name": name, "value": xml_text(value, 300)})
    classname = xml_text(report.suite.name if report.suite else report.project.name, 200)
    for result in report.results:
        case = ET.SubElement(
            suite,
            "testcase",
            {
                "name": xml_text(f"{result.case_key} {result.title}", 300),
                "classname": classname,
                "time": f"{(result.duration_ms or 0) / 1000:.3f}",
            },
        )
        bad = _failed_step(result)
        detail = "\n".join(
            line
            for line in (
                f"Category: {result.failure_category}" if result.failure_category else "",
                f"Step {bad.position}: {bad.description or bad.action}" if bad else "",
                f"Expected: {bad.expected}" if bad and bad.expected is not None else "",
                f"Found: {bad.actual}" if bad and bad.actual is not None else "",
                bad.error_message if bad and bad.error_message else "",
            )
            if line
        )
        message = xml_text(result.failure_summary or result.status, 500)
        if result.status == "failed":
            node = ET.SubElement(
                case, "failure", {"message": message, "type": result.failure_category or "failed"}
            )
            node.text = xml_text(detail)
        elif result.status == "error":
            node = ET.SubElement(
                case, "error", {"message": message, "type": result.failure_category or "error"}
            )
            node.text = xml_text(detail)
        elif result.status in ("flaky", "needs_review"):
            node = ET.SubElement(case, "failure", {"message": message, "type": result.status})
            node.text = xml_text(
                "Passed only after a retry."
                if result.status == "flaky"
                else "Needs a person's review."
            )
        elif result.status != "passed":
            ET.SubElement(case, "skipped", {"message": message})
    body: bytes = ET.tostring(suite, encoding="utf-8")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + body
