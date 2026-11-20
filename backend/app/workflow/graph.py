"""The graph: fifteen nodes, a wait for clarification, and the edges between them.

    receive_instruction
      ├─ existing suite ───────────────────────────────┐
      ▼                                                │
    analyse_requirement ◄── request_clarification      │
      │      └─ incomplete ──────────▲                 │
      ▼ complete                                       ▼
    validate_application ──────────────────────► (existing suite) ─┐
      ▼                                                            │
    generate_test_plan                                             │
      ▼                                                            │
    generate_test_cases ◄─┐                                        │
      ▼                   │ scripts invalid, within the limit      │
    validate_test_cases ──┘                                        │
      ▼ valid, or limit reached                                    │
    approval_checkpoint ◄──────────────────────────────────────────┘
      │  waits if approval is required; rejected ──► generate_report
      ▼ approved or not required
    prepare_execution
      ▼
    execute_test_cases ◄──────────────┐
      ▼                               │
    verify_test_results               │
      ├─ all passed ──► collect_evidence
      ▼ something did not pass        │
    analyse_failures                  │
      ▼                               │
    retry_eligible_tests ─ retry allowed ┘
      ▼ not allowed
    collect_evidence ──► generate_report ──► save_execution_results ──► END

Any node that fails, and a cancellation, sends the run to `generate_report`,
so every run ends with a report and a saved outcome.
"""

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.core.errors import AppError
from app.core.metrics import metrics
from app.models import WorkflowRun
from app.workflow import authoring, routing, running
from app.workflow.authoring import run_log
from app.workflow.deps import WorkflowDeps, WorkflowError
from app.workflow.state import NODES, WAIT_FOR_CLARIFICATION, WorkflowState

logger = logging.getLogger(__name__)

NodeFunction = Callable[[WorkflowState, WorkflowDeps], Awaitable[dict[str, Any]]]
# Nodes that still run after a cancellation or a failure: they close the run.
_CLOSING = frozenset({"collect_evidence", "generate_report", "save_execution_results"})

FUNCTIONS: dict[str, NodeFunction] = {
    "receive_instruction": authoring.receive_instruction,
    "analyse_requirement": authoring.analyse_requirement,
    WAIT_FOR_CLARIFICATION: authoring.request_clarification,
    "validate_application": authoring.validate_application,
    "generate_test_plan": authoring.generate_test_plan,
    "generate_test_cases": authoring.generate_test_cases,
    "validate_test_cases": authoring.validate_test_cases,
    "approval_checkpoint": authoring.approval_checkpoint,
    "prepare_execution": running.prepare_execution,
    "execute_test_cases": running.execute_test_cases,
    "verify_test_results": running.verify_test_results,
    "analyse_failures": running.analyse_failures,
    "retry_eligible_tests": running.retry_eligible_tests,
    "collect_evidence": running.collect_evidence,
    "generate_report": running.generate_report,
    "save_execution_results": running.save_execution_results,
}
assert set(NODES) <= set(FUNCTIONS)  # noqa: S101 - every required node is implemented


def instrument(
    name: str, function: NodeFunction, deps: WorkflowDeps
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Wrap a node: record when it starts and ends, notice cancellation, contain its errors."""

    async def node(state: WorkflowState) -> dict[str, Any]:
        log = run_log(state, deps)
        started = time.monotonic()
        cancelling = await _enter(deps, state, name)
        if cancelling and name not in _CLOSING and state.get("status", "running") == "running":
            await log.write("node_end", name, status="cancelled", latency_ms=0)
            return {"status": "cancelled"}
        await log.write("node_start", name)
        try:
            update = await function(state, deps)
        except GraphBubbleUp:
            # Not an error: the node is waiting for a person.
            await log.write("interrupt", name, latency_ms=_ms(started))
            raise
        except (WorkflowError, AppError) as exc:
            update = _failed(
                name, str(getattr(exc, "code", "error")), str(getattr(exc, "message", exc))
            )
        except Exception:
            logger.exception(
                "workflow_node_failed", extra={"run_id": state["run_id"], "node": name}
            )
            update = _failed(name, "internal_error", "The step failed unexpectedly.")
        failed = update.get("status") == "failed"
        if failed:
            await metrics.inc("qa_agent_node_failures_total", {"node": name})
            await log.write(
                "error", name, status="failed", error=(update.get("errors") or [{}])[-1]
            )
        await log.write(
            "node_end", name, status=str(update.get("status") or "ok"), latency_ms=_ms(started)
        )
        return update

    node.__name__ = name
    return node


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _failed(node: str, code: str, message: str) -> dict[str, Any]:
    return {"status": "failed", "errors": [{"node": node, "code": code, "message": message}]}


async def _enter(deps: WorkflowDeps, state: WorkflowState, node: str) -> bool:
    """Note which node is running. Returns whether cancellation has been requested."""
    async with deps.transaction(state["organization_id"]) as session:
        run = await session.get(WorkflowRun, uuid.UUID(state["run_id"]))
        if run is None:
            return False
        cancelling = run.status == "cancelling"
        run.current_node = node
        run.heartbeat_at = datetime.now(UTC)
        if not cancelling:
            run.status = "running"
        return cancelling


def build_graph(deps: WorkflowDeps) -> StateGraph[WorkflowState]:
    graph: StateGraph[WorkflowState] = StateGraph(WorkflowState)
    for name, function in FUNCTIONS.items():
        action: Any = instrument(name, function, deps)
        graph.add_node(name, action)

    stop = routing.STOP
    graph.add_edge(START, "receive_instruction")
    graph.add_conditional_edges(
        "receive_instruction",
        routing.after_receive,
        ["analyse_requirement", "validate_application", stop],
    )
    graph.add_conditional_edges(
        "analyse_requirement",
        routing.after_analysis,
        [WAIT_FOR_CLARIFICATION, "validate_application", stop],
    )
    graph.add_conditional_edges(
        WAIT_FOR_CLARIFICATION,
        routing.proceed("analyse_requirement"),
        ["analyse_requirement", stop],
    )
    graph.add_conditional_edges(
        "validate_application",
        routing.after_application,
        ["generate_test_plan", "approval_checkpoint", stop],
    )
    graph.add_conditional_edges(
        "generate_test_plan", routing.proceed("generate_test_cases"), ["generate_test_cases", stop]
    )
    graph.add_conditional_edges(
        "generate_test_cases", routing.proceed("validate_test_cases"), ["validate_test_cases", stop]
    )
    graph.add_conditional_edges(
        "validate_test_cases",
        routing.after_case_validation,
        ["generate_test_cases", "approval_checkpoint", stop],
    )
    graph.add_conditional_edges(
        "approval_checkpoint", routing.after_approval, ["prepare_execution", stop]
    )
    graph.add_conditional_edges(
        "prepare_execution", routing.proceed("execute_test_cases"), ["execute_test_cases", stop]
    )
    graph.add_conditional_edges(
        "execute_test_cases", routing.proceed("verify_test_results"), ["verify_test_results", stop]
    )
    graph.add_conditional_edges(
        "verify_test_results",
        routing.after_verification,
        ["analyse_failures", "collect_evidence", stop],
    )
    graph.add_conditional_edges(
        "analyse_failures",
        routing.proceed("retry_eligible_tests"),
        ["retry_eligible_tests", stop],
    )
    graph.add_conditional_edges(
        "retry_eligible_tests",
        routing.after_retry_decision,
        ["execute_test_cases", "collect_evidence"],
    )
    graph.add_edge("collect_evidence", "generate_report")
    graph.add_conditional_edges("generate_report", routing.after_report, ["save_execution_results"])
    graph.add_edge("save_execution_results", END)
    return graph


def compile_graph(
    deps: WorkflowDeps, checkpointer: BaseCheckpointSaver[str]
) -> CompiledStateGraph[Any, Any, Any, Any]:
    return build_graph(deps).compile(checkpointer=checkpointer)
