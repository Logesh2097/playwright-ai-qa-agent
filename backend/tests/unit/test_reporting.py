"""Evidence rules, download tokens, the report and its exports, and the analytics. No database."""

import csv
import io
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jwt
import pytest

from app.core.errors import UnauthenticatedError
from app.core.security.tokens import create_access_token
from app.reporting import analytics
from app.reporting.analytics import CaseFacts, ExecutionRow, ResultRow
from app.reporting.builder import assemble, pass_rate
from app.reporting.exports import CSV_COLUMNS, csv_cell, export, pdf_text, to_csv, to_json, to_pdf
from app.reporting.model import ExecutionReport
from app.services.evidence import (
    EvidenceLimits,
    EvidenceRejectedError,
    EvidenceStore,
    validate,
)
from app.services.evidence_access import DownloadClaims, sign_download, verify_download

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"
WEBM = b"\x1a\x45\xdf\xa3" + b"frames"
ZIP = b"PK\x03\x04" + b"trace"
LIMITS = EvidenceLimits(
    screenshot_bytes=1000, recording_bytes=5000, log_bytes=200, execution_bytes=6000,
    retention_days=30, recording_retention_days=14,
)  # fmt: skip
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)

# --- file validation ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "content_type", "data"),
    [
        ("screenshot", "image/png", PNG),
        ("video", "video/webm", WEBM),
        ("trace", "application/zip", ZIP),
        ("console_log", "application/json", b'{"messages": []}'),
        ("network_log", "application/json", b"[]"),
    ],
)
def test_evidence_that_is_what_it_says_is_accepted(
    kind: str, content_type: str, data: bytes
) -> None:
    validate(kind, content_type, data, LIMITS)


@pytest.mark.parametrize(
    ("kind", "content_type", "data", "code"),
    [
        ("screenshot", "image/png", b"<html><script>alert(1)</script>", "invalid_content"),
        ("screenshot", "image/png", b"MZ\x90\x00 an executable", "invalid_content"),
        ("screenshot", "image/png", WEBM, "invalid_content"),
        ("video", "video/webm", PNG, "invalid_content"),
        ("trace", "application/zip", b"not a zip", "invalid_content"),
        ("console_log", "application/json", b"{not json", "invalid_content"),
        ("console_log", "application/json", b'"just a string"', "invalid_content"),
        ("console_log", "application/json", b"\xff\xfe", "invalid_content"),
        ("screenshot", "text/html", PNG, "wrong_content_type"),
        ("screenshot", "image/svg+xml", b"<svg/>", "wrong_content_type"),
        ("executable", "application/octet-stream", b"x", "unknown_kind"),
        ("dom_snapshot", "text/html", b"<html>", "unknown_kind"),
        ("screenshot", "image/png", b"", "empty"),
        ("screenshot", "image/png", PNG + b"x" * 1000, "too_large"),
        ("console_log", "application/json", b"[" + b"1," * 150 + b"1]", "too_large"),
    ],
)
def test_evidence_that_is_not_what_it_says_or_too_large_is_refused(
    kind: str, content_type: str, data: bytes, code: str
) -> None:
    with pytest.raises(EvidenceRejectedError) as refused:
        validate(kind, content_type, data, LIMITS)
    assert refused.value.code == code


def test_recordings_may_be_larger_and_are_kept_for_less_long() -> None:
    validate("video", "video/webm", WEBM + b"x" * 3000, LIMITS)
    assert LIMITS.expires_at("screenshot", NOW) == NOW + timedelta(days=30)
    assert LIMITS.expires_at("console_log", NOW) == NOW + timedelta(days=30)
    assert LIMITS.expires_at("video", NOW) == NOW + timedelta(days=14)
    assert LIMITS.expires_at("trace", NOW) == NOW + timedelta(days=14)


def test_the_store_keeps_organizations_apart_and_never_leaves_its_root(tmp_path: Path) -> None:
    store = EvidenceStore(str(tmp_path))
    ours, theirs, execution = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    stored = store.put(ours, execution, "image/png", PNG)
    (tmp_path / "outside.txt").write_text("secret")

    assert stored.key.startswith(f"{ours}/{execution}/") and stored.key.endswith(".png")
    assert store.read(ours, stored.key) == PNG
    assert b"".join(store.chunks(ours, stored.key) or []) == PNG
    # Another organization cannot read it, by its key or by walking up the tree.
    assert store.read(theirs, stored.key) is None and store.chunks(theirs, stored.key) is None
    for key in (
        "../outside.txt",
        f"{ours}/../outside.txt",
        "/etc/passwd",
        f"{theirs}/../{stored.key}",
    ):
        assert store.read(theirs, key) is None, key
    assert store.read(ours, "../outside.txt") is None
    assert store.organizations() == [ours]
    assert store.delete(theirs, stored.key) is False and store.read(ours, stored.key) == PNG
    assert store.delete(ours, stored.key) is True and store.read(ours, stored.key) is None
    assert not (tmp_path / str(ours) / str(execution)).exists()


# --- download addresses -------------------------------------------------------------------

SECRET = "s" * 40
CLAIMS = DownloadClaims(
    organization_id=uuid.uuid4(), project_id=uuid.uuid4(), execution_id=uuid.uuid4(),
    evidence_id=uuid.uuid4(), user_id=uuid.uuid4(), session_id=uuid.uuid4(),
)  # fmt: skip


def test_a_download_token_names_one_file_one_user_and_one_session() -> None:
    token, expires = sign_download(CLAIMS, secret=SECRET, issuer="qa", ttl_s=300)
    assert verify_download(token, secret=SECRET, issuer="qa") == CLAIMS
    assert timedelta(seconds=290) < expires - datetime.now(UTC) <= timedelta(seconds=300)


def test_expired_forged_and_other_kinds_of_token_are_refused() -> None:
    expired, _ = sign_download(CLAIMS, secret=SECRET, issuer="qa", ttl_s=-5)
    good, _ = sign_download(CLAIMS, secret=SECRET, issuer="qa", ttl_s=300)
    head, body, signature = good.split(".")
    payload = jwt.decode(good, SECRET, algorithms=["HS256"], issuer="qa")
    retargeted = jwt.encode(
        {**payload, "evi": str(uuid.uuid4())},
        "another-secret-of-enough-length-000",
        algorithm="HS256",
    )
    unsigned = jwt.encode(payload, "", algorithm="none")
    # A sign-in token is signed with the same key and must not open a file.
    access = create_access_token(
        user_id=CLAIMS.user_id, session_id=CLAIMS.session_id, secret=SECRET, ttl_s=300, issuer="qa"
    )
    for bad in (
        expired,
        retargeted,
        unsigned,
        access,
        f"{head}.{body}.AAAA{signature[4:]}",
        "x.y.z",
        "",
    ):
        with pytest.raises(UnauthenticatedError):
            verify_download(bad, secret=SECRET, issuer="qa")
    with pytest.raises(UnauthenticatedError):
        verify_download(good, secret=SECRET, issuer="someone-else")


# --- the report ---------------------------------------------------------------------------


def _ns(**values: Any) -> Any:
    return SimpleNamespace(**values)


def _step(result_id: uuid.UUID, position: int, action: str, status: str, **more: Any) -> Any:
    values = {
        "id": uuid.uuid4(), "result_id": result_id, "attempt": 1, "position": position, "phase": "main",
        "action": action, "description": f"Step {position}", "status": status, "started_at": NOW,
        "finished_at": NOW, "duration_ms": 100, "resolved_locator": None, "healed_from": None,
        "expected": None, "actual": None, "error_type": None, "error_message": None, "page_url": "https://app.test/",
    }  # fmt: skip
    return _ns(**{**values, **more})


def _result(case: Any, version: Any, status: str, **more: Any) -> Any:
    values = {
        "id": uuid.uuid4(), "test_case_id": case.id, "test_case_version_id": version.id, "status": status,
        "failure_category": None, "failure_summary": None, "healed": False, "attempt_count": 1,
        "started_at": NOW, "finished_at": NOW + timedelta(seconds=2), "duration_ms": 2000,
        "details": {}, "ai_analysis": None,
    }  # fmt: skip
    return _ns(**{**values, **more})


def sample_report(**overrides: Any) -> tuple[ExecutionReport, dict[str, Any]]:
    """A report over seven results, one of every status, built from stand-in rows."""
    organization = uuid.uuid4()
    cases, versions, results, steps, evidence = {}, {}, [], {}, []
    statuses = ["passed", "failed", "error", "skipped", "blocked", "flaky", "needs_review"]
    for number, status in enumerate(statuses, start=1):
        case = _ns(id=uuid.uuid4(), key=f"TC-{number:04d}", title=f"Case {number}")
        version = _ns(
            id=uuid.uuid4(),
            title=f"Case {number}",
            version=2,
            expected_result="50 points are shown",
        )
        cases[case.id], versions[version.id] = case, version
        result = _result(case, version, status)
        results.append(result)
        steps[result.id] = [_step(result.id, 1, "navigate", "passed")]
    failed, errored = results[1], results[2]
    failed.failure_category, failed.failure_summary = (
        "test_assertion_failure",
        "expected 50, found 51",
    )
    failed.details = {
        "console_errors": ["Uncaught TypeError"], "page_errors": [],
        "failed_requests": [{"url": "/api/points", "status": 500}],
        "blocked_requests": [{"url": "https://tracker.example/"}],
        "verification": {"assertions_evaluated": 1},
    }  # fmt: skip
    failed.ai_analysis = {"summary": "The page showed 51.", "possible_root_cause": "Possibly rounding.",
                          "impact": "Wrong points.", "debugging_steps": ["Check the rule."], "advisory": True}  # fmt: skip
    bad = _step(failed.id, 2, "assert_text", "failed", expected={"equals": "50"}, actual={"text": "51"},
                error_type="assertion", error_message="expected 50, found 51")  # fmt: skip
    steps[failed.id].append(bad)
    errored.failure_category, errored.failure_summary = (
        "test_automation_failure",
        "No element matches",
    )
    shot = _ns(id=uuid.uuid4(), result_id=failed.id, execution_step_id=bad.id, kind="screenshot",
               content_type="image/png", size_bytes=10, expires_at=NOW + timedelta(days=5),
               redaction_status="not_required")  # fmt: skip
    gone = _ns(id=uuid.uuid4(), result_id=failed.id, execution_step_id=None, kind="video",
               content_type="video/webm", size_bytes=10, expires_at=NOW - timedelta(days=1),
               redaction_status="not_required")  # fmt: skip
    held = _ns(id=uuid.uuid4(), result_id=failed.id, execution_step_id=None, kind="console_log",
               content_type="application/json", size_bytes=10, expires_at=None,
               redaction_status="failed_quarantined")  # fmt: skip
    evidence = [shot, gone, held]
    rows: dict[str, Any] = {
        "project": _ns(id=uuid.uuid4(), name="Web shop"),
        "application": _ns(id=uuid.uuid4(), name="Storefront"),
        "environment": _ns(id=uuid.uuid4(), name="staging", kind="staging", base_url="https://staging.acme.io"),
        "instruction": _ns(id=uuid.uuid4(), raw_text="Check that loyalty points follow the rule."),
        "suite": _ns(id=uuid.uuid4(), name="Loyalty"),
        "execution": _ns(
            id=uuid.uuid4(), status="completed", verdict="failed", started_at=NOW,
            finished_at=NOW + timedelta(seconds=75), triggered_by=uuid.uuid4(),
            config={"browser": "chromium"}, error=None, organization_id=organization,
        ),
        "triggered_by": _ns(full_name="Ada Lovelace", email="ada@acme.io"),
        "results": results, "cases": cases, "versions": versions, "steps": steps, "evidence": evidence,
        "now": NOW,
    }  # fmt: skip
    rows.update(overrides)
    return assemble(**rows), {"failed": failed, "shot": shot, "bad": bad, "rows": rows}


def test_the_report_counts_every_status_from_the_results_and_names_what_was_tested() -> None:
    report, _ = sample_report()

    assert report.totals.model_dump() == {
        "total": 7, "passed": 1, "failed": 1, "skipped": 1, "blocked": 1, "error": 1, "flaky": 1,
        "needs_review": 1,
        # Of the five that ran to an end, one passed. Skipped and blocked are not in the rate.
        "pass_rate": 0.2,
    }  # fmt: skip
    assert (report.project.name, report.application.name, report.suite.name) == (  # type: ignore[union-attr]
        "Web shop", "Storefront", "Loyalty",
    )  # fmt: skip
    assert (
        report.environment is not None and report.environment.base_url == "https://staging.acme.io"
    )
    assert report.instruction is not None and "loyalty points" in report.instruction.text
    execution = report.execution
    assert (execution.status, execution.verdict, execution.duration_ms) == (
        "completed",
        "failed",
        75_000,
    )
    assert (execution.started_at, execution.finished_at) == (NOW, NOW + timedelta(seconds=75))
    assert execution.triggered_by == "Ada Lovelace" and execution.browser == "chromium"
    assert report.failure_categories == {"test_assertion_failure": 1, "test_automation_failure": 1}
    assert [r.status for r in report.results] == [
        "passed", "failed", "error", "skipped", "blocked", "flaky", "needs_review",
    ]  # fmt: skip


def test_failures_carry_the_step_expected_actual_screenshots_and_a_labelled_suggestion() -> None:
    report, made = sample_report()

    assert [f.status for f in report.failures] == ["failed", "error"]  # not flaky, not needs_review
    failure = report.failures[0]
    assert failure.summary == "expected 50, found 51" and failure.failed_step == "Step 2: Step 2"
    assert (failure.expected, failure.actual) == ({"equals": "50"}, {"text": "51"})
    assert failure.screenshot_ids == [made["shot"].id]
    detail = report.results[1]
    assert [s.position for s in detail.steps] == [1, 2] and detail.steps[
        1
    ].error_type == "assertion"
    assert detail.console_errors == ["Uncaught TypeError"]
    assert detail.network_errors == [
        {"url": "/api/points", "status": 500},
        {"url": "https://tracker.example/", "blocked": True},
    ]
    # Expired and quarantined files are not offered.
    assert [(e.kind, e.step_position) for e in detail.evidence] == [("screenshot", 2)]
    assert (
        any("1 evidence file(s) are no longer available" in note for note in report.notes) is False
    )
    assert any("2 evidence file(s) are no longer available" in note for note in report.notes)
    suggestion = detail.ai_suggestion
    assert suggestion is not None and suggestion["advisory"] is True
    assert "Not a confirmed root cause" in suggestion["note"]
    assert report.results[0].ai_suggestion is None
    assert any("does not measure code coverage" in note for note in report.notes)


def test_a_report_without_optional_parts_and_an_unfinished_execution() -> None:
    _, made = sample_report()
    rows = made["rows"]
    running = _ns(
        **{**vars(rows["execution"]), "status": "running", "verdict": None, "finished_at": None}
    )
    report, _ = sample_report(
        application=None, environment=None, instruction=None, suite=None, triggered_by=None,
        execution=running, results=[], evidence=[],
    )  # fmt: skip
    assert (report.application, report.environment, report.instruction, report.suite) == (None,) * 4
    assert report.totals.total == 0 and report.totals.pass_rate is None
    assert report.execution.duration_ms is None and report.execution.verdict is None
    assert any("provisional" in note for note in report.notes)
    assert pass_rate({"passed": 3, "failed": 1, "skipped": 6}) == 0.75


# --- exports ------------------------------------------------------------------------------


def test_json_export_is_the_report_itself() -> None:
    report, _ = sample_report()
    parsed = json.loads(to_json(report))
    assert ExecutionReport.model_validate(parsed) == report
    assert parsed["totals"]["failed"] == 1 and parsed["report_version"] == 1


def test_csv_export_has_one_row_per_result_and_cannot_start_a_formula() -> None:
    report, made = sample_report()
    made["rows"]["cases"][made["failed"].test_case_id].key = "TC-0002"
    hostile = report.model_copy(deep=True)
    hostile.results[1].title = '=HYPERLINK("http://evil.example","click")'
    hostile.results[1].failure_summary = "+cmd|' /C calc'!A0"
    hostile.results[2].failure_summary = "@SUM(1+1)"
    hostile.results[0].title = "-2+3"

    data = to_csv(hostile)

    assert data.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    assert tuple(rows[0]) == CSV_COLUMNS and len(rows) == 1 + 7
    by_column = [dict(zip(CSV_COLUMNS, row, strict=True)) for row in rows[1:]]
    assert [r["status"] for r in by_column] == [r.status for r in report.results]
    failed = by_column[1]
    assert failed["title"].startswith("'=HYPERLINK") and failed["failure_summary"].startswith(
        "'+cmd"
    )
    assert by_column[2]["failure_summary"].startswith("'@") and by_column[0]["title"] == "'-2+3"
    for row in by_column:
        assert not any(cell.startswith(("=", "+", "-", "@")) for cell in row.values())
    assert failed["failed_step"] == "2: Step 2" and json.loads(failed["expected"]) == {
        "equals": "50"
    }
    assert failed["ai_suggestion_summary"] == "[AI suggestion, unconfirmed] The page showed 51."
    assert (failed["environment"], failed["suite"], failed["healed"]) == (
        "staging",
        "Loyalty",
        "no",
    )
    assert (
        csv_cell(None) == ""
        and csv_cell(True) == "yes"
        and csv_cell({"b": 1, "a": 2}) == '{"a": 2, "b": 1}'
    )


def test_pdf_export_contains_the_summary_the_failures_and_the_labelled_suggestion() -> None:
    report, made = sample_report()

    data = to_pdf(report, {str(made["shot"].id): b"not really a png"}, compress=False)

    assert data.startswith(b"%PDF-") and data.rstrip().endswith(b"%%EOF")
    for text in (
        b"Test report", b"Web shop", b"Storefront", b"staging", b"Loyalty", b"FAILED",
        b"Results that did not pass", b"TC-0002", b"expected 50, found 51", b"test assertion failure",
        b"AI-generated suggestion", b"Not a confirmed root cause", b"Possibly rounding.",
        b"A screenshot could not be embedded.", b"does not measure code coverage",
    ):  # fmt: skip
        assert text in data, text
    # A passing case has no section of its own.
    assert data.count(b"Case 1") == 1


def test_pdf_export_copes_with_text_its_fonts_cannot_show() -> None:
    report, _ = sample_report()
    odd = report.model_copy(deep=True)
    odd.results[1].title = "ポイント “quoted” → 50 \x00\x07 " + "x" * 5000
    odd.results[1].failure_summary = "a" * 400 + "https://example.test/" + "b" * 900
    assert to_pdf(odd).startswith(b"%PDF-")
    assert pdf_text("“hi” … \x01") == '"hi" ...  '
    assert len(pdf_text("x" * 5000)) == 2003


def test_every_export_format_has_its_type_and_name() -> None:
    report, _ = sample_report()
    found = {fmt: export(report, fmt) for fmt in ("json", "csv", "pdf")}
    assert found["json"].media_type == "application/json"
    assert (
        found["csv"].media_type.startswith("text/csv")
        and found["pdf"].media_type == "application/pdf"
    )
    assert found["pdf"].filename == f"test-report-{report.execution.id}.pdf"
    with pytest.raises(KeyError):
        export(report, "html")


# --- analytics ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base", "target", "change"),
    [
        ("passed", "failed", "regressed"),
        ("passed", "error", "regressed"),
        ("flaky", "failed", "regressed"),
        ("skipped", "failed", "regressed"),
        ("failed", "passed", "fixed"),
        ("error", "passed", "fixed"),
        ("failed", "failed", "still_failing"),
        ("failed", "error", "still_failing"),
        ("passed", "passed", "unchanged"),
        ("passed", "flaky", "changed"),
        ("failed", "flaky", "changed"),  # not a fix: it did not pass first time
        ("failed", "needs_review", "changed"),
        ("passed", "skipped", "changed"),
        (None, "passed", "added"),
        ("failed", None, "removed"),
    ],
)
def test_how_a_case_changed_between_two_executions(
    base: str | None, target: str | None, change: str
) -> None:
    assert analytics.change_of(base, target) == change


def _row(case: str, status: str, execution: str = "e", **more: Any) -> ResultRow:
    return ResultRow(id=uuid.uuid4(), execution_id=execution, case_id=case, status=status,
                     finished_at=more.pop("finished_at", NOW), **more)  # fmt: skip


def test_comparison_lists_regressions_first_and_counts_each_kind_of_change() -> None:
    base = [
        _row("a", "passed"),
        _row("b", "failed"),
        _row("c", "passed"),
        _row("d", "failed"),
        _row("gone", "passed"),
    ]
    target = [
        _row("a", "failed"),
        _row("b", "passed"),
        _row("c", "passed"),
        _row("d", "failed"),
        _row("new", "passed"),
    ]

    rows, counts = analytics.compare(base, target)

    assert counts == {"regressed": 1, "fixed": 1, "still_failing": 1, "changed": 0, "added": 1,
                      "removed": 1, "unchanged": 1}  # fmt: skip
    assert [(r["case_id"], r["change"]) for r in rows][:3] == [
        ("a", "regressed"), ("d", "still_failing"), ("b", "fixed"),
    ]  # fmt: skip
    assert sum(counts.values()) == 6


def _execution(
    day: int, verdict: str | None, seconds: int = 60, key: str | None = None
) -> ExecutionRow:
    start = datetime(2026, 11, day, 9, 0, tzinfo=UTC)
    return ExecutionRow(id=key or f"e{day}{verdict}", status="completed" if verdict else "running", verdict=verdict,
                        created_at=start, started_at=start,
                        finished_at=start + timedelta(seconds=seconds) if verdict else None)  # fmt: skip


def test_the_trend_has_one_point_per_day_and_empty_days_are_zero_not_missing() -> None:
    executions = [_execution(17, "passed", 60, "x"), _execution(17, "failed", 120, "y"), _execution(19, "passed", 30, "z"),
                  _execution(1, "passed", key="old")]  # fmt: skip
    results = [_row("a", "passed", "x"), _row("b", "passed", "x"), _row("a", "failed", "y"),
               _row("b", "skipped", "y"), _row("a", "passed", "z"), _row("a", "failed", "old")]  # fmt: skip

    points = analytics.trend(executions, results, days=3, today=date(2026, 11, 19))

    assert [p["date"] for p in points] == ["2026-11-17", "2026-11-18", "2026-11-19"]
    first, empty, last = points
    assert (first["executions"], first["verdicts"]) == (2, {"passed": 1, "failed": 1})
    assert (first["results"]["ran"], first["results"]["pass_rate"], first["results"]["failure_rate"]) == (
        3, 0.6667, 0.3333,
    )  # fmt: skip
    assert first["results"]["skipped"] == 1 and first["average_duration_ms"] == 90_000
    assert (empty["executions"], empty["results"]["pass_rate"], empty["average_duration_ms"]) == (
        0,
        None,
        None,
    )
    assert (last["executions"], last["results"]["pass_rate"]) == (1, 1.0)


def test_dashboard_figures_are_what_their_definitions_say() -> None:
    executions = [_execution(17, "passed", 60), _execution(18, "failed", 120), _execution(18, "flaky", 30, "f"),
                  _execution(19, "error", 30), _execution(19, None, key="running")]  # fmt: skip
    hours = iter(range(100))

    def at(case: str, status: str, **more: Any) -> ResultRow:
        return _row(
            case,
            status,
            finished_at=NOW + timedelta(hours=next(hours)),
            version_id="v",
            environment_id="env",
            **more,
        )

    results = [
        *(at("steady", "passed") for _ in range(4)),
        *(at("broken", "failed", failure_category="test_assertion_failure") for _ in range(4)),
        at("wobbly", "passed"), at("wobbly", "error", failure_category="network_failure"),
        at("wobbly", "passed"), at("wobbly", "error", failure_category="network_failure"), at("wobbly", "passed"),
        at("retried", "flaky"), at("never", "skipped"),
    ]  # fmt: skip
    cases = [
        CaseFacts("steady", "TC-1", "Steady", "approved", approved=True, automated=True),
        CaseFacts("broken", "TC-2", "Broken", "approved", approved=True, automated=True),
        CaseFacts("wobbly", "TC-3", "Wobbly", "approved", approved=True, automated=True),
        CaseFacts("retried", "TC-4", "Retried", "approved", approved=True, automated=True),
        CaseFacts("never", "TC-5", "Never run", "approved", approved=True, automated=True),
        CaseFacts("manual", "TC-6", "Manual only", "approved", approved=True, automated=False),
        CaseFacts("draft", "TC-7", "Draft", "needs_review", approved=False),
        CaseFacts("old", "TC-8", "Old", "archived", approved=True, automated=True),
    ]

    found = analytics.dashboard(executions, results, cases, days=30)

    assert (found["total_executions"], found["finished_executions"]) == (5, 4)
    assert (
        found["executions_by_verdict"]["passed"] == 1
        and found["executions_by_verdict"]["flaky"] == 1
    )
    # One of four finished executions passed; a flaky execution is not a pass.
    assert (found["execution_pass_rate"], found["execution_failure_rate"]) == (0.25, 0.5)
    rates = found["results"]
    assert (rates["ran"], rates["passed"], rates["failed"], rates["error"], rates["flaky"]) == (
        14,
        7,
        4,
        2,
        1,
    )
    assert (rates["pass_rate"], rates["failure_rate"]) == (0.5, 0.4286)
    assert found["average_execution_duration_ms"] == 60_000
    assert found["flaky_test_count"] == 2 and set(found["flaky_case_ids"]) == {"wobbly", "retried"}
    failing = found["frequently_failing"]
    assert [(f["case_key"], f["failures"], f["runs"]) for f in failing] == [
        ("TC-2", 4, 4),
        ("TC-3", 2, 5),
    ]
    assert (
        failing[0]["failure_rate"] == 1.0
        and failing[0]["most_common_category"] == "test_assertion_failure"
    )
    assert failing[1]["last_status"] == "passed"
    coverage = found["coverage"]
    assert (coverage["test_cases"], coverage["approved"], coverage["automated"]) == (7, 6, 5)
    assert (coverage["executed"], coverage["passing"]) == (4, 2)
    assert (coverage["automated_share"], coverage["executed_share"], coverage["passing_share"]) == (
        0.8333,
        0.8,
        0.4,
    )
    assert "not code coverage" in coverage["note"]


def test_nothing_run_gives_no_rates_rather_than_zero_or_a_hundred_percent() -> None:
    found = analytics.dashboard([], [], [], days=7)
    assert (
        found["execution_pass_rate"],
        found["results"]["pass_rate"],
        found["results"]["failure_rate"],
    ) == (None,) * 3
    assert found["average_execution_duration_ms"] is None and found["flaky_test_count"] == 0
    assert found["coverage"]["automated_share"] is None and found["frequently_failing"] == []
