"""The workflow state.

It holds identifiers and compact summaries. The real data (analysis, plan,
cases, results, evidence) lives in its own tables and is loaded by ID.

Nothing secret is ever put here: no credential values, no tokens, no page
content, no screenshots. The state is serialized into checkpoints, so anything
in it is stored at rest and readable by whoever can read the database.
"""

import operator
from typing import Annotated, Any, Literal, TypedDict

NODES = (
    "receive_instruction",
    "analyse_requirement",
    "validate_application",
    "generate_test_plan",
    "generate_test_cases",
    "validate_test_cases",
    "approval_checkpoint",
    "prepare_execution",
    "execute_test_cases",
    "verify_test_results",
    "analyse_failures",
    "retry_eligible_tests",
    "collect_evidence",
    "generate_report",
    "save_execution_results",
)
WAIT_FOR_CLARIFICATION = "request_clarification"

Mode = Literal["generate_and_run", "run_existing"]
# running: carry on. The others end the run; `generate_report` is still reached.
Status = Literal["running", "failed", "rejected", "cancelled", "completed"]
ApprovalReason = Literal[
    "generated_test_cases",
    "state_changing_operations",
    "destructive_cleanup",
    "outside_default_scope",
]
ToolChoice = Literal["playwright_engine", "api_engine", "mcp_read_only", "none"]


class Options(TypedDict):
    max_retries: int
    max_regenerations: int
    analyse_failures: bool
    discover_application: bool
    # A `RunConfig`, as plain data.
    run_config: dict[str, Any]


class RequirementSummary(TypedDict):
    status: str
    ready: bool
    feature_name: str | None
    objectives: int
    open_questions: int
    blocking_issues: list[str]


class PageSummary(TypedDict):
    url: str
    title: str | None
    # (role, accessible name) of elements a test could act on or check.
    elements: list[list[str]]


class ApplicationCheck(TypedDict):
    valid: bool
    # How the application was looked at: "mcp_read_only", or why it was not.
    inspected_with: str
    pages: list[PageSummary]
    notes: list[str]


class ToolResultSummary(TypedDict):
    tool: str
    status: str
    page_url: str | None
    error_code: str | None


class PlanSummary(TypedDict):
    plan_id: str
    version: int
    objectives: int
    # objective id -> which tool runs its cases
    tool_selection: dict[str, ToolChoice]
    discovery: ToolChoice


class CaseSummary(TypedDict):
    case_id: str
    key: str
    title: str
    version: int
    ready: bool
    has_script: bool
    tool: ToolChoice
    blocking_issues: list[str]


class ApprovalState(TypedDict):
    required: bool
    reasons: list[ApprovalReason]
    approval_id: str | None
    decision: Literal["approved", "rejected"] | None
    decided_by: str | None
    note: str | None


class ApprovedCase(TypedDict):
    case_id: str
    key: str
    version_id: str


class SessionRef(TypedDict):
    """Which browser work belongs to this run. A reference, never a session token."""

    kind: Literal["engine_run"]
    id: str
    browser: str
    viewport: str


class ResultSummary(TypedDict):
    result_id: str
    execution_id: str
    case_id: str
    key: str
    status: str
    failure_class: str | None
    failure_summary: str | None


class ValidationSummary(TypedDict):
    execution_id: str
    execution_status: str
    verdict: str
    totals: dict[str, int]
    # Results whose stored steps do not support their status; they are not counted as passed.
    inconsistent: list[str]


class FailureDetail(TypedDict):
    result_id: str
    case_id: str
    key: str
    status: str
    failure_class: str | None
    error_type: str | None
    summary: str | None
    retry_eligible: bool
    retry_reason: str
    # Whether a model's explanation was stored with the result. Advisory only.
    analysed: bool


class AttemptRecord(TypedDict):
    attempt: int
    execution_id: str
    execution_status: str
    verdict: str
    cases: int
    retried: list[str]


class EvidenceRef(TypedDict):
    evidence_id: str
    execution_id: str
    result_id: str | None
    kind: str


class ReportSummary(TypedDict):
    status: Literal["ready", "failed"]
    report_id: str | None
    verdict: str | None


class NodeError(TypedDict):
    node: str
    code: str
    message: str


class WorkflowState(TypedDict, total=False):
    # identity
    run_id: str
    mode: Mode
    organization_id: str
    project_id: str
    application_id: str
    environment_id: str
    user_id: str
    instruction_id: str | None
    suite_id: str | None
    options: Options

    # requirement
    user_instruction: str
    requirement_analysis: RequirementSummary | None
    clarification_rounds: int

    # application and plan
    application: ApplicationCheck | None
    mcp_tool_results: Annotated[list[ToolResultSummary], operator.add]
    test_plan: PlanSummary | None

    # cases
    generated_test_cases: list[CaseSummary]
    regeneration_count: int
    # case key -> why its last script could not be used. Sent back if scripts are written again.
    script_problems: dict[str, list[str]]
    approval: ApprovalState | None
    approved_test_cases: list[ApprovedCase]
    excluded_test_cases: list[CaseSummary]

    # execution
    execution_id: str | None
    browser_session: SessionRef | None
    execution_results: list[ResultSummary]
    validation_results: ValidationSummary | None
    failure_details: list[FailureDetail]
    retry_count: int
    # Keys of the cases the next execution runs again.
    pending_retry: list[str]
    attempts: Annotated[list[AttemptRecord], operator.add]

    # outcome
    evidence_refs: list[EvidenceRef]
    final_report: ReportSummary | None
    status: Status
    errors: Annotated[list[NodeError], operator.add]


def initial_state(
    *,
    run_id: str,
    mode: Mode,
    organization_id: str,
    project_id: str,
    environment_id: str,
    user_id: str,
    instruction_id: str | None,
    suite_id: str | None,
    options: Options,
) -> WorkflowState:
    return WorkflowState(
        run_id=run_id,
        mode=mode,
        organization_id=organization_id,
        project_id=project_id,
        environment_id=environment_id,
        user_id=user_id,
        instruction_id=instruction_id,
        suite_id=suite_id,
        options=options,
        clarification_rounds=0,
        regeneration_count=0,
        retry_count=0,
        mcp_tool_results=[],
        generated_test_cases=[],
        approved_test_cases=[],
        excluded_test_cases=[],
        execution_results=[],
        failure_details=[],
        attempts=[],
        evidence_refs=[],
        errors=[],
        status="running",
    )
