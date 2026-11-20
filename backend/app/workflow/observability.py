"""What is recorded about a run as it happens, and how it is added up.

Everything goes into `agent_execution_logs`: one row when a node starts, one
when it ends (with its duration and outcome), one per model call (with token
counts), one per MCP tool call, one per interrupt, resume and error.
"""

import logging
import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.llm.base import LLMCallRecord
from app.db.context import set_current_org
from app.models import AgentExecutionLog
from app.services.llm_usage import record_llm_metrics

logger = logging.getLogger(__name__)


class RunLog:
    """Writes trace rows for one run, each in its own short transaction."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        organization_id: uuid.UUID,
        run_id: uuid.UUID,
        instruction_id: uuid.UUID | None,
    ) -> None:
        self._session_factory = session_factory
        self._organization_id = organization_id
        self._run_id = run_id
        self._instruction_id = instruction_id

    async def write(
        self,
        event_type: str,
        node: str,
        *,
        status: str | None = None,
        latency_ms: int | None = None,
        error: dict[str, Any] | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        try:
            async with self._session_factory() as session, session.begin():
                await set_current_org(session, self._organization_id)
                session.add(
                    AgentExecutionLog(
                        organization_id=self._organization_id,
                        thread_id=str(self._run_id),
                        instruction_id=self._instruction_id,
                        node=node,
                        event_type=event_type,
                        status=status,
                        latency_ms=latency_ms,
                        error=error,
                        tool_args=detail,
                    )
                )
        except Exception:
            # Losing a trace row must not stop the workflow.
            logger.exception("workflow_log_failed", extra={"run_id": str(self._run_id)})

    def llm_recorder(self, node: str) -> "LLMRecorder":
        return LLMRecorder(self, node)

    async def llm_call(self, node: str, record: LLMCallRecord) -> None:
        try:
            async with self._session_factory() as session, session.begin():
                await set_current_org(session, self._organization_id)
                session.add(
                    AgentExecutionLog(
                        organization_id=self._organization_id,
                        thread_id=str(self._run_id),
                        instruction_id=self._instruction_id,
                        node=node,
                        event_type="llm_call",
                        model=record.model,
                        prompt_tokens=record.usage.input_tokens,
                        completion_tokens=record.usage.output_tokens,
                        cost_usd=record.usage.cost_usd,
                        latency_ms=record.latency_ms,
                        tool_name=record.prompt,
                        status=record.status,
                        error=None if record.status == "ok" else {"code": record.status},
                    )
                )
        except Exception:
            logger.exception("workflow_log_failed", extra={"run_id": str(self._run_id)})


class LLMRecorder:
    def __init__(self, log: RunLog, node: str) -> None:
        self._log = log
        self._node = node

    async def __call__(self, record: LLMCallRecord) -> None:
        await record_llm_metrics(record)
        await self._log.llm_call(self._node, record)


def summarize(logs: Sequence[AgentExecutionLog]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Totals for a run, and its node history in order."""
    calls = [row for row in logs if row.event_type == "llm_call"]
    tools = [row for row in logs if row.event_type == "tool_call"]
    cost = sum((row.cost_usd for row in calls if row.cost_usd is not None), Decimal(0))
    usage = {
        "llm_calls": len(calls),
        "llm_calls_failed": sum(1 for row in calls if row.status != "ok"),
        "input_tokens": sum(row.prompt_tokens or 0 for row in calls),
        "output_tokens": sum(row.completion_tokens or 0 for row in calls),
        "cost_usd": str(cost) if any(row.cost_usd is not None for row in calls) else None,
        "mcp_calls": len(tools),
        "mcp_calls_refused": sum(1 for row in tools if row.status in ("denied", "blocked")),
        "interrupts": sum(1 for row in logs if row.event_type == "interrupt"),
        "errors": sum(1 for row in logs if row.event_type == "error"),
    }
    nodes = [
        {
            "node": row.node,
            "event": row.event_type,
            "status": row.status,
            "duration_ms": row.latency_ms,
            "at": row.created_at.isoformat() if row.created_at else None,
            "error": row.error,
        }
        for row in logs
        if row.event_type in ("node_start", "node_end", "interrupt", "resume", "error")
    ]
    return usage, nodes
