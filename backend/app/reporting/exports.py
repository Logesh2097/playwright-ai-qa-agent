"""Renderings of an `ExecutionReport`: JSON, CSV and PDF.

All three take the same report object and add nothing to it. Text that came
from the application under test is written as plain text only: it is never
interpreted as markup, and in CSV a cell can never start a spreadsheet formula.
"""

import csv
import io
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from app.reporting.model import SUGGESTION_NOTE, ExecutionReport, ResultDetail

FORMATS: dict[str, tuple[str, str]] = {
    "json": ("application/json", ".json"),
    "csv": ("text/csv; charset=utf-8", ".csv"),
    "pdf": ("application/pdf", ".pdf"),
}
MAX_PDF_SCREENSHOTS = 12
MAX_PDF_STEPS = 60
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True, slots=True)
class Export:
    content: bytes
    media_type: str
    filename: str


# --- JSON ---------------------------------------------------------------------------------


def to_json(report: ExecutionReport) -> bytes:
    return report.model_dump_json(indent=2).encode()


# --- CSV ----------------------------------------------------------------------------------

CSV_COLUMNS = (
    "execution_id", "project", "application", "environment", "suite", "case_key", "title",
    "version", "status", "failure_category", "failure_summary", "healed", "attempts",
    "started_at", "finished_at", "duration_ms", "steps", "failed_step", "expected", "actual",
    "error", "evidence_files", "ai_suggestion_summary",
)  # fmt: skip


def csv_cell(value: Any) -> str:
    """Text for one cell. A leading =, +, - or @ would be run as a formula by a spreadsheet."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping | list):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    text = str(value).replace("\x00", "")
    return "'" + text if text.startswith(_FORMULA_START) else text


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


def to_csv(report: ExecutionReport) -> bytes:
    """One row per test case result."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for result in report.results:
        bad = _failed_step(result)
        suggestion = result.ai_suggestion or {}
        row = {
            "execution_id": report.execution.id,
            "project": report.project.name,
            "application": report.application.name if report.application else None,
            "environment": report.environment.name if report.environment else None,
            "suite": report.suite.name if report.suite else None,
            "case_key": result.case_key,
            "title": result.title,
            "version": result.version,
            "status": result.status,
            "failure_category": result.failure_category,
            "failure_summary": result.failure_summary,
            "healed": result.healed,
            "attempts": result.attempts,
            "started_at": result.started_at,
            "finished_at": result.finished_at,
            "duration_ms": result.duration_ms,
            "steps": len({(s.attempt, s.position) for s in result.steps}),
            "failed_step": f"{bad.position}: {bad.description or bad.action}" if bad else None,
            "expected": bad.expected if bad else None,
            "actual": bad.actual if bad else None,
            "error": bad.error_message if bad else None,
            "evidence_files": len(result.evidence),
            "ai_suggestion_summary": (
                f"[AI suggestion, unconfirmed] {suggestion['summary']}"
                if suggestion.get("summary")
                else None
            ),
        }
        writer.writerow([csv_cell(row[column]) for column in CSV_COLUMNS])
    # The byte-order mark makes spreadsheet programs read the file as UTF-8.
    return b"\xef\xbb\xbf" + buffer.getvalue().encode()


# --- PDF ----------------------------------------------------------------------------------

# Characters the built-in fonts lack, by code point: quotes, dashes, ellipsis, arrow,
# no-break space, check mark, bullet.
_REPLACEMENTS = {
    0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"', 0x2013: "-", 0x2014: "-",
    0x2026: "...", 0x2192: "->", 0x00A0: " ", 0x2714: "x", 0x2022: "*",
}  # fmt: skip
_STATUS_COLOURS: dict[str, tuple[int, int, int]] = {
    "passed": (22, 120, 60),
    "failed": (176, 32, 32),
    "error": (160, 82, 0),
    "flaky": (130, 100, 0),
    "needs_review": (90, 60, 150),
}


def pdf_text(value: Any, limit: int = 2000) -> str:
    """Text the built-in PDF fonts can show: Latin-1, no control characters, bounded."""
    if isinstance(value, Mapping | list):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    text = "" if value is None else str(value)
    text = text.translate(_REPLACEMENTS)
    text = "".join(ch if ch in "\n\t" or ord(ch) >= 32 else " " for ch in text)
    text = text.encode("latin-1", "replace").decode("latin-1")
    return text if len(text) <= limit else text[:limit] + "..."


def _time(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S UTC") if value else "-"


def _duration(ms: int | None) -> str:
    if ms is None:
        return "-"
    seconds = ms / 1000
    return f"{seconds:.1f} s" if seconds < 60 else f"{int(seconds // 60)} min {int(seconds % 60)} s"


class _Document(FPDF):
    def __init__(self, title: str) -> None:
        super().__init__(format="A4")
        self._title = title
        self.set_auto_page_break(auto=True, margin=15)
        self.set_margins(15, 15, 15)
        self.set_title(title)
        self.set_creator("QA Agent")

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("Helvetica", size=8)
        self.set_text_color(110, 110, 110)
        self.cell(0, 6, f"{self._title} - page {self.page_no()}", align="C")
        self.set_text_color(0, 0, 0)

    def heading(self, text: str, size: int = 13) -> None:
        self.ln(3)
        self.set_font("Helvetica", style="B", size=size)
        self.set_x(self.l_margin)
        self.multi_cell(0, 7, pdf_text(text, 200), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_font("Helvetica", size=10)

    def line_of(self, label: str, value: Any) -> None:
        self.set_x(self.l_margin)
        self.set_font("Helvetica", style="B", size=10)
        self.cell(42, 5.5, pdf_text(label, 60))
        self.set_font("Helvetica", size=10)
        self.multi_cell(0, 5.5, pdf_text(value, 600) or "-", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def paragraph(self, text: Any, *, size: int = 10, limit: int = 2000) -> None:
        self.set_x(self.l_margin)
        self.set_font("Helvetica", size=size)
        self.multi_cell(0, 5, pdf_text(text, limit) or "-", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_font("Helvetica", size=10)


def to_pdf(
    report: ExecutionReport,
    screenshots: Mapping[str, bytes] | None = None,
    *,
    compress: bool = True,
) -> bytes:
    """`screenshots` maps evidence IDs to PNG bytes; only those given are embedded."""
    screenshots = screenshots or {}
    pdf = _Document(f"Test report {str(report.execution.id)[:8]}")
    pdf.set_compression(compress)
    pdf.add_page()
    pdf.heading("Test report", 18)
    pdf.paragraph(f"Generated {_time(report.generated_at)}", size=9)

    execution = report.execution
    pdf.heading("Execution")
    pdf.line_of("Final status", (execution.verdict or "not finished").replace("_", " ").upper())
    pdf.line_of("Lifecycle", execution.status)
    pdf.line_of("Execution ID", execution.id)
    pdf.line_of("Project", report.project.name)
    pdf.line_of("Application", report.application.name if report.application else None)
    environment = report.environment
    pdf.line_of(
        "Environment",
        f"{environment.name} ({environment.kind}) {environment.base_url}" if environment else None,
    )
    pdf.line_of("Test suite", report.suite.name if report.suite else None)
    pdf.line_of("Started", _time(execution.started_at))
    pdf.line_of("Finished", _time(execution.finished_at))
    pdf.line_of("Duration", _duration(execution.duration_ms))
    pdf.line_of("Started by", execution.triggered_by)
    pdf.line_of("Browser", execution.browser)
    if report.instruction:
        pdf.heading("Testing instruction")
        pdf.paragraph(report.instruction.text, limit=3000)

    totals = report.totals
    pdf.heading("Summary")
    rate = f"{totals.pass_rate * 100:.1f} %" if totals.pass_rate is not None else "-"
    with pdf.table(col_widths=(1,) * 9, text_align="CENTER", line_height=6) as table:
        header = table.row()
        for name in ("Total", "Passed", "Failed", "Error", "Skipped", "Blocked", "Flaky", "Review", "Pass rate"):  # fmt: skip
            header.cell(name)
        row = table.row()
        for value in (totals.total, totals.passed, totals.failed, totals.error, totals.skipped,
                      totals.blocked, totals.flaky, totals.needs_review, rate):  # fmt: skip
            row.cell(str(value))
    if report.failure_categories:
        pdf.ln(2)
        pdf.paragraph(
            "Failure categories (decided by rules): "
            + ", ".join(
                f"{k.replace('_', ' ')}: {v}" for k, v in sorted(report.failure_categories.items())
            )
        )

    pdf.heading("Test cases")
    with pdf.table(col_widths=(14, 50, 14, 22), line_height=5.5) as table:
        header = table.row()
        for name in ("Case", "Title", "Status", "Duration"):
            header.cell(name)
        for result in report.results:
            row = table.row()
            row.cell(pdf_text(result.case_key, 40))
            row.cell(pdf_text(result.title, 160))
            row.cell(result.status.replace("_", " "))
            row.cell(_duration(result.duration_ms))

    embedded = 0
    not_passed = [r for r in report.results if r.status not in ("passed", "skipped", "blocked")]
    if not_passed:
        pdf.add_page()
        pdf.heading("Results that did not pass", 15)
    for result in not_passed:
        pdf.heading(f"{result.case_key}  {result.title}", 12)
        pdf.set_text_color(*_STATUS_COLOURS.get(result.status, (0, 0, 0)))
        pdf.line_of("Status", result.status.replace("_", " "))
        pdf.set_text_color(0, 0, 0)
        if result.failure_category:
            pdf.line_of("Category", result.failure_category.replace("_", " ") + " (by rules)")
        pdf.line_of("Summary", result.failure_summary)
        pdf.line_of("Expected result", result.expected_result)
        pdf.line_of("Attempts", result.attempts)
        last = max((step.attempt for step in result.steps), default=0)
        steps = [step for step in result.steps if step.attempt == last][:MAX_PDF_STEPS]
        if steps:
            pdf.ln(1)
            with pdf.table(col_widths=(6, 14, 46, 12, 12), line_height=5) as table:
                header = table.row()
                for name in ("#", "Action", "Step", "Status", "Time"):
                    header.cell(name)
                for step in steps:
                    row = table.row()
                    row.cell(str(step.position))
                    row.cell(pdf_text(step.action, 40))
                    row.cell(pdf_text(step.description or "", 140))
                    row.cell(step.status)
                    row.cell(_duration(step.duration_ms))
        bad = _failed_step(result)
        if bad is not None:
            pdf.ln(1)
            pdf.line_of("Failed step", f"{bad.position}: {bad.description or bad.action}")
            pdf.line_of("Expected", bad.expected)
            pdf.line_of("Found", bad.actual)
            pdf.line_of("Error", bad.error_message)
            pdf.line_of("Page", bad.page_url)
        if result.console_errors or result.page_errors:
            pdf.line_of(
                "Console errors", "\n".join([*result.console_errors, *result.page_errors][:8])
            )
        if result.network_errors:
            pdf.line_of("Network errors", result.network_errors[:8])
        suggestion = result.ai_suggestion
        if suggestion:
            pdf.ln(1)
            pdf.set_font("Helvetica", style="B", size=10)
            pdf.set_x(pdf.l_margin)
            pdf.multi_cell(0, 5.5, "AI-generated suggestion", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.paragraph(SUGGESTION_NOTE, size=8)
            pdf.line_of("Summary", suggestion.get("summary"))
            pdf.line_of("Possible cause", suggestion.get("possible_root_cause"))
            pdf.line_of("Impact", suggestion.get("impact"))
            for index, step_text in enumerate(suggestion.get("debugging_steps") or [], start=1):
                pdf.line_of(f"Next step {index}", step_text)
        for item in result.evidence:
            data = screenshots.get(str(item.id))
            if item.kind != "screenshot" or data is None or embedded >= MAX_PDF_SCREENSHOTS:
                continue
            try:
                if pdf.get_y() > 170:
                    pdf.add_page()
                pdf.ln(2)
                pdf.image(
                    io.BytesIO(data), x=pdf.l_margin, w=pdf.epw, h=100, keep_aspect_ratio=True
                )
                pdf.ln(2)
                embedded += 1
            except Exception:
                pdf.paragraph("(A screenshot could not be embedded.)", size=8)

    pdf.heading("Notes")
    for note in report.notes:
        pdf.paragraph(f"- {note}", size=9)
    return bytes(pdf.output())


def export(
    report: ExecutionReport, fmt: str, screenshots: Mapping[str, bytes] | None = None
) -> Export:
    media_type, extension = FORMATS[fmt]
    content = (
        to_json(report)
        if fmt == "json"
        else to_csv(report)
        if fmt == "csv"
        else to_pdf(report, screenshots)
    )
    return Export(content, media_type, f"test-report-{report.execution.id}{extension}")
