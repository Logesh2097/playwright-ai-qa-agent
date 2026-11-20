"""Conditional edges. Each is a pure function of the state: no I/O, no model.

Every router first asks whether the run should stop. A node that failed, a
cancellation or a rejection sends the run to `generate_report`, so that even a
run that ends early leaves a report of what happened.
"""

from app.workflow.state import WorkflowState

STOP = "generate_report"


def stopped(state: WorkflowState) -> bool:
    return state.get("status", "running") != "running"


def after_receive(state: WorkflowState) -> str:
    """An existing approved suite needs no analysis or generation."""
    if stopped(state):
        return STOP
    return "validate_application" if state["mode"] == "run_existing" else "analyse_requirement"


def after_analysis(state: WorkflowState) -> str:
    """Requirement complete / incomplete."""
    if stopped(state):
        return STOP
    summary = state.get("requirement_analysis")
    if summary is None:
        return STOP
    if summary["status"] == "needs_clarification":
        return "request_clarification"
    return "validate_application" if summary["ready"] else STOP


def after_application(state: WorkflowState) -> str:
    if stopped(state):
        return STOP
    application = state.get("application")
    if application is None or not application["valid"]:
        return STOP
    return "approval_checkpoint" if state["mode"] == "run_existing" else "generate_test_plan"


def after_case_validation(state: WorkflowState) -> str:
    """Test cases valid / invalid. Invalid scripts are regenerated a bounded number of times."""
    if stopped(state):
        return STOP
    cases = state.get("generated_test_cases", [])
    if not cases:
        return STOP
    needs_script_work = any(_script_problem(case["blocking_issues"]) for case in cases)
    if (
        needs_script_work
        and state.get("regeneration_count", 0) < state["options"]["max_regenerations"]
    ):
        return "generate_test_cases"
    return "approval_checkpoint"


def _script_problem(issues: list[str]) -> bool:
    # Only what another generation pass can fix. A missing expected result, for
    # example, needs a person and is not a reason to ask the model again.
    return any(code.startswith("automation_") or code == "no_script" for code in issues)


def after_approval(state: WorkflowState) -> str:
    """Approval required / not required, and approved / rejected."""
    if stopped(state):
        return STOP
    approval = state.get("approval")
    if approval is None:
        return STOP
    if approval["required"] and approval["decision"] != "approved":
        return STOP
    return "prepare_execution" if state.get("approved_test_cases") else STOP


def after_verification(state: WorkflowState) -> str:
    """Browser execution successful / failed, and tests passed / failed."""
    if stopped(state):
        return STOP
    validation = state.get("validation_results")
    if validation is None:
        return STOP
    if state.get("failure_details"):
        return "analyse_failures"
    return "collect_evidence"


def after_retry_decision(state: WorkflowState) -> str:
    """Retry allowed / not allowed.

    When a retry is allowed the node has prepared the next execution and made it
    the current one. An execution that no attempt has recorded yet is one to run.
    """
    if stopped(state):
        return "collect_evidence"
    recorded = {attempt["execution_id"] for attempt in state.get("attempts", [])}
    current = state.get("execution_id")
    return (
        "execute_test_cases"
        if current is not None and current not in recorded
        else "collect_evidence"
    )


def after_report(state: WorkflowState) -> str:
    """Report generated / failed. Results are saved either way."""
    return "save_execution_results"


def proceed(next_node: str):  # type: ignore[no-untyped-def]
    """An edge that continues to `next_node` unless the run has stopped."""

    def route(state: WorkflowState) -> str:
        return STOP if stopped(state) else next_node

    route.__name__ = f"to_{next_node}"
    return route
