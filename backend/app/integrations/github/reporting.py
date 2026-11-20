"""What is written back to GitHub: a commit status and one pull request comment.

Both are built from the test report and the CI outcome. Neither is written
unless the repository link has that switched on.

Text that came from a test run (titles, expected and found values, messages) is
untrusted here too. In the comment it is placed inside code spans or with
Markdown escaped, so it cannot become a link, an image, HTML or a mention of a
GitHub user.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.reporting.ci import CiOutcome
from app.reporting.model import ExecutionReport, ResultDetail

MARKER = "<!-- qa-agent:test-report -->"
MAX_COMMENT_CHARS = 60_000
MAX_FAILURES = 10
MAX_DEBUG_STEPS = 3
STATUS_CONTEXT = "QA Agent"
_MARKDOWN = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~&])")
# After an @, so that GitHub does not turn a name into a mention and notify that person.
_NO_MENTION = "@\u200b"

# What a person can do next, by the category the rules assigned. Used when no AI
# suggestion exists; shown as general guidance, not as a diagnosis.
NEXT_STEPS: dict[str, list[str]] = {
    "test_assertion_failure": [
        "Compare the expected and found values below with what this change does.",
        "Open the report and look at the screenshot taken at the failing step.",
        "If the new behaviour is intended, update the test case and have it approved.",
    ],
    "application_failure": [
        "Check the application's logs for the server error or script error recorded in the report.",
        "Reproduce the failing step against the same environment.",
    ],
    "test_automation_failure": [
        "The test could not perform a step (often a renamed or removed element). Check whether this change touches that part of the page.",
        "If the page changed on purpose, update the test script; a replacement locator may be proposed in the platform.",
    ],
    "browser_infrastructure_failure": [
        "This is a failure of the test infrastructure, not of the application. Re-run the tests.",
    ],
    "network_failure": [
        "The environment could not be reached. Check that it is deployed and running, then re-run.",
    ],
    "authentication_failure": [
        "Signing in with the test account failed. Check the test account and the login page of the environment.",
    ],
    "inconclusive": [
        "The result says nothing reliable yet. Open the report for the reason, then re-run.",
    ],
}


def md(text: Any, limit: int = 300) -> str:
    """Untrusted text as inert Markdown: one line, specials escaped, bounded."""
    value = " ".join(str(text if text is not None else "").split())
    if len(value) > limit:
        value = value[:limit] + "…"
    return _MARKDOWN.sub(r"\\\1", value).replace("@", _NO_MENTION)


def code(text: Any, limit: int = 300) -> str:
    """Untrusted text in a code span. Backticks cannot end the span early."""
    value = " ".join(str(text if text is not None else "").split()).replace("`", "'")
    if len(value) > limit:
        value = value[:limit] + "…"
    return f"`{value}`" if value else "—"


@dataclass(frozen=True, slots=True)
class CommitStatus:
    state: str  # GitHub's: pending, success, failure, error
    description: str


def commit_status(outcome: CiOutcome) -> CommitStatus:
    """Only a verified pass is `success`."""
    if not outcome.finished:
        return CommitStatus("pending", "Tests are running.")
    if outcome.outcome == "passed":
        return CommitStatus("success", outcome.summary)
    if outcome.outcome == "failed":
        return CommitStatus("failure", outcome.summary)
    return CommitStatus("error" if outcome.outcome == "incomplete" else "failure", outcome.summary)


def status_context(suite_name: str | None) -> str:
    return f"{STATUS_CONTEXT} / {suite_name}"[:255] if suite_name else STATUS_CONTEXT


@dataclass(frozen=True, slots=True)
class RunSummary:
    """One execution as it appears in a pull request comment."""

    report: ExecutionReport
    outcome: CiOutcome
    report_url: str | None


_ICON = {"passed": "✅", "failed": "❌", "unverified": "⚠️", "incomplete": "⚠️", "running": "⏳"}


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


def _failure_block(result: ResultDetail) -> list[str]:
    lines = [f"**{md(result.case_key, 40)}** {md(result.title, 160)} — {md(result.status, 20)}"]
    if result.failure_category:
        lines.append(f"- Category (by rules): {md(result.failure_category.replace('_', ' '), 60)}")
    bad = _failed_step(result)
    if bad is not None:
        lines.append(f"- Step {bad.position}: {md(bad.description or bad.action, 200)}")
        if bad.expected is not None:
            lines.append(f"- Expected: {code(bad.expected)}")
        if bad.actual is not None:
            lines.append(f"- Found: {code(bad.actual)}")
        if bad.error_message:
            lines.append(f"- Error: {code(bad.error_message)}")
    elif result.failure_summary:
        lines.append(f"- {md(result.failure_summary, 300)}")
    suggestion = result.ai_suggestion or {}
    steps = [str(s) for s in (suggestion.get("debugging_steps") or [])][:MAX_DEBUG_STEPS]
    if steps:
        lines.append(
            "- Suggested debugging steps (AI-generated suggestion, not a confirmed root cause):"
        )
        lines += [f"  - {md(step, 300)}" for step in steps]
    else:
        general = NEXT_STEPS.get(result.failure_category or "", [])[:MAX_DEBUG_STEPS]
        if general:
            lines.append("- Suggested next steps:")
            lines += [f"  - {step}" for step in general]
    return lines


def pr_comment(
    runs: Sequence[RunSummary], *, head_sha: str, selection: dict[str, Any] | None = None
) -> str:
    """One comment for a commit, covering every suite that ran for it."""
    worst = "passed"
    for name in ("running", "incomplete", "unverified", "failed"):
        if any(run.outcome.outcome == name for run in runs):
            worst = name
    headline = {
        "passed": "All tests passed",
        "failed": "Tests failed",
        "unverified": "Tests did not give a verified pass",
        "incomplete": "Tests did not finish",
        "running": "Tests are running",
    }[worst]
    lines = [MARKER, f"## {_ICON[worst]} QA Agent: {headline}", "", f"Commit `{head_sha[:12]}`", ""]
    lines += [
        "| Suite | Result | Total | Passed | Failed | Error | Skipped | Other | Report |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for run in runs:
        totals = run.report.totals
        other = totals.flaky + totals.needs_review + totals.blocked
        name = run.report.suite.name if run.report.suite else "Selected cases"
        link = f"[Open]({run.report_url})" if run.report_url else "—"
        lines.append(
            f"| {md(name, 80)} | {_ICON[run.outcome.outcome]} {run.outcome.outcome} | {totals.total} | "
            f"{totals.passed} | {totals.failed} | {totals.error} | {totals.skipped} | {other} | {link} |"
        )
    lines.append("")
    if selection:
        scope = (
            "the suites this change concerns"
            if selection.get("scope") == "affected"
            else "the full set"
        )
        lines += [f"Ran {scope}. {md(selection.get('reason', ''), 300)}", ""]

    shown = 0
    failing = [
        (run, result)
        for run in runs
        for result in run.report.results
        if result.status in ("failed", "error")
    ]
    if failing:
        lines += ["### Failed tests", ""]
        for _, result in failing[:MAX_FAILURES]:
            lines += [*_failure_block(result), ""]
            shown += 1
        if len(failing) > shown:
            lines += [f"…and {len(failing) - shown} more. See the reports.", ""]
    attention = [
        result
        for run in runs
        for result in run.report.results
        if result.status in ("flaky", "needs_review")
    ]
    if attention:
        lines += ["### Not counted as passed", ""]
        for result in attention[:MAX_FAILURES]:
            why = (
                "passed only after a retry"
                if result.status == "flaky"
                else "needs a person's review"
            )
            lines.append(f"- **{md(result.case_key, 40)}** {md(result.title, 160)} — {why}")
        lines.append("")
    lines += [
        "Screenshots and other evidence are in the reports and require signing in to QA Agent "
        "with access to the project.",
        "",
        "<sub>Results are decided by the tests' own assertions. Categories are assigned by fixed rules.</sub>",
    ]
    body = "\n".join(lines)
    if len(body) > MAX_COMMENT_CHARS:
        body = body[:MAX_COMMENT_CHARS] + "\n\n…shortened. See the reports."
    return body
