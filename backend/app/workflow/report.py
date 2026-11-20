"""The final report of a workflow run, built from stored facts.

Counts, statuses and the verdict are computed here from the results table. A
model's explanation of a failure is included where one was stored, in a field
of its own that says it is advice; it has no part in any number or status.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from app.models import TestCase, TestEvidence, TestExecution, TestExecutionStep, TestResult
from app.testing.verdicts import execution_verdict, totals
from app.workflow.state import WorkflowState


def final_statuses(
    executions: Sequence[TestExecution], results: Sequence[TestResult]
) -> dict[Any, tuple[TestResult, str, int]]:
    """Per case: its last result, the status it ends with, and how many attempts it had.

    A case that passed only when the workflow ran it again is flaky, not passed.
    """
    order = {execution.id: index for index, execution in enumerate(executions)}
    by_case: dict[Any, list[TestResult]] = {}
    for result in sorted(results, key=lambda r: order.get(r.execution_id, 0)):
        by_case.setdefault(result.test_case_id, []).append(result)
    final: dict[Any, tuple[TestResult, str, int]] = {}
    for case_id, attempts in by_case.items():
        last = attempts[-1]
        status = last.status
        if status == "passed" and len(attempts) > 1:
            status = "flaky"
        final[case_id] = (last, status, len(attempts))
    return final


def build_report(
    state: WorkflowState,
    *,
    environment: Mapping[str, Any],
    executions: Sequence[TestExecution],
    results: Sequence[TestResult],
    cases: Mapping[Any, TestCase],
    steps: Mapping[Any, Sequence[TestExecutionStep]],
    evidence: Sequence[TestEvidence],
    usage: Mapping[str, Any],
    nodes: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    final = final_statuses(executions, results)
    statuses = [status for _, status, _ in final.values()]
    evidence_by_result: dict[Any, list[str]] = {}
    for item in evidence:
        evidence_by_result.setdefault(item.result_id, []).append(str(item.id))

    result_entries = []
    for case_id, (result, status, attempts) in final.items():
        case = cases.get(case_id)
        entry: dict[str, Any] = {
            "case_id": str(case_id),
            "case_key": case.key if case else None,
            "title": case.title if case else None,
            "status": status,
            "attempts": attempts,
            "failure_class": result.failure_class,
            # Decided by rules from what was recorded, not by a model.
            "failure_category": result.failure_category,
            "failure_summary": result.failure_summary,
            "healed": result.healed,
            "duration_ms": result.duration_ms,
            "evidence_ids": evidence_by_result.get(result.id, []),
        }
        bad = next((s for s in steps.get(result.id, []) if s.status in ("failed", "error")), None)
        if bad is not None:
            entry["step_that_did_not_pass"] = {
                "position": bad.position,
                "phase": bad.phase,
                "action": bad.action,
                "description": bad.description,
                "status": bad.status,
                "expected": bad.expected,
                "actual": bad.actual,
                "error_type": bad.error_type,
                "error_message": bad.error_message,
            }
        replaced = [
            {
                "position": s.position,
                "action": s.action,
                "from": s.healed_from,
                "to": s.resolved_locator,
            }
            for s in steps.get(result.id, [])
            if s.healed_from
        ]
        if replaced:
            entry["replaced_locators"] = replaced
        if result.ai_analysis:
            entry["explanation"] = {
                **result.ai_analysis,
                "note": "AI-generated suggestion, not a confirmed root cause. It does not affect any status.",
            }
        result_entries.append(entry)

    ran = bool(executions)
    application = state.get("application")
    run_status = state.get("status", "running")
    return {
        "run_id": state["run_id"],
        "mode": state["mode"],
        "generated_at": datetime.now(UTC).isoformat(),
        "outcome": "completed" if run_status == "running" else run_status,
        # No execution, no verdict: nothing was verified.
        "verdict": execution_verdict(statuses) if ran else None,
        "totals": totals(statuses),
        "failure_categories": dict(
            Counter(r.failure_category for r, _, _ in final.values() if r.failure_category)
        ),
        "instruction": state.get("user_instruction"),
        "environment": dict(environment),
        "requirement": state.get("requirement_analysis"),
        "application": {
            "inspected_with": application["inspected_with"] if application else None,
            "pages_seen": len(application["pages"]) if application else 0,
            "notes": application["notes"] if application else [],
        },
        "plan": state.get("test_plan"),
        "cases": {
            "generated": len(state.get("generated_test_cases", [])),
            "approved": len(state.get("approved_test_cases", [])),
            "excluded": [
                {"case_key": c["key"], "title": c["title"], "reasons": c["blocking_issues"]}
                for c in state.get("excluded_test_cases", [])
            ],
        },
        "approval": state.get("approval"),
        "attempts": state.get("attempts", []),
        "retries": state.get("retry_count", 0),
        "results": result_entries,
        "evidence": {"count": len(evidence), "kinds": sorted({item.kind for item in evidence})},
        "usage": dict(usage),
        "nodes": [dict(node) for node in nodes],
        "errors": state.get("errors", []),
    }
