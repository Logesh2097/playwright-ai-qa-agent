"""What is recorded about a browser session. Everything here is already sanitized."""

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from app.core.metrics import metrics

logger = logging.getLogger(__name__)

CallStatus = Literal["ok", "error", "timeout", "denied", "blocked"]
SessionEventKind = Literal["connecting", "connected", "reconnected", "closed", "failed"]


@dataclass(frozen=True, slots=True)
class ToolCallEvent:
    session_id: uuid.UUID
    sequence: int
    tool: str
    category: str | None
    arguments: dict[str, Any]
    status: CallStatus
    duration_ms: int
    error_code: str | None = None
    error_message: str | None = None
    page_url: str | None = None
    result_summary: str | None = None


@dataclass(frozen=True, slots=True)
class SessionEvent:
    session_id: uuid.UUID
    kind: SessionEventKind
    detail: dict[str, Any] = field(default_factory=dict)


class Recorder(Protocol):
    async def tool_call(self, event: ToolCallEvent) -> None: ...

    async def session(self, event: SessionEvent) -> None: ...


class LogRecorder:
    """Writes one structured log line per event."""

    async def tool_call(self, event: ToolCallEvent) -> None:
        await metrics.add(
            {
                f'qa_mcp_tool_calls_total{{tool="{event.tool}",status="{event.status}"}}': 1,
                f'qa_mcp_tool_duration_seconds_sum{{tool="{event.tool}"}}': (event.duration_ms or 0)
                / 1000,
                f'qa_mcp_tool_duration_seconds_count{{tool="{event.tool}"}}': 1,
            }
        )
        logger.info(
            "mcp_tool_call",
            extra={
                "browser_session_id": str(event.session_id),
                "sequence": event.sequence,
                "tool": event.tool,
                "tool_category": event.category,
                "tool_arguments": event.arguments,
                "status": event.status,
                "duration_ms": event.duration_ms,
                "error_code": event.error_code,
                "page_url": event.page_url,
            },
        )

    async def session(self, event: SessionEvent) -> None:
        logger.info(
            "mcp_session",
            extra={
                "browser_session_id": str(event.session_id),
                "connection_status": event.kind,
                **{f"session_{key}": value for key, value in event.detail.items()},
            },
        )


class Recorders:
    """Fans out to several recorders. One failing never stops a browser action."""

    def __init__(self, *recorders: Recorder) -> None:
        self._recorders = recorders

    async def tool_call(self, event: ToolCallEvent) -> None:
        for recorder in self._recorders:
            try:
                await recorder.tool_call(event)
            except Exception:
                logger.exception("mcp_recorder_failed")

    async def session(self, event: SessionEvent) -> None:
        for recorder in self._recorders:
            try:
                await recorder.session(event)
            except Exception:
                logger.exception("mcp_recorder_failed")
