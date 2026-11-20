"""Token and cost accounting for LLM calls."""

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.llm.base import LLMCallRecord
from app.core.config import Settings
from app.core.errors import RateLimitedError
from app.core.metrics import metrics
from app.db.context import set_current_org
from app.models import AgentExecutionLog
from app.repositories.instructions import AgentLogRepository

logger = logging.getLogger(__name__)


class LLMUsageRecorder:
    """Writes one usage-log row per provider call, in its own transaction.

    A separate transaction, so that the record of a failed or timed-out call
    survives whatever happens to the work that caused it.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        organization_id: uuid.UUID,
        instruction_id: uuid.UUID,
        node: str,
    ) -> None:
        self._session_factory = session_factory
        self._organization_id = organization_id
        self._instruction_id = instruction_id
        self._node = node

    async def __call__(self, record: LLMCallRecord) -> None:
        await record_llm_metrics(record)
        try:
            async with self._session_factory() as session, session.begin():
                await set_current_org(session, self._organization_id)
                session.add(
                    AgentExecutionLog(
                        organization_id=self._organization_id,
                        thread_id=str(self._instruction_id),
                        instruction_id=self._instruction_id,
                        node=self._node,
                        event_type="llm_call",
                        model=record.model,
                        prompt_tokens=record.usage.input_tokens,
                        completion_tokens=record.usage.output_tokens,
                        cost_usd=record.usage.cost_usd,
                        latency_ms=record.latency_ms,
                        # Which prompt version produced this call; not a tool, but the same idea.
                        tool_name=record.prompt,
                        status=record.status,
                        error=None if record.status == "ok" else {"code": record.status},
                    )
                )
        except Exception:
            # Accounting must never break the analysis itself.
            logger.exception("llm_usage_write_failed")


async def require_budget(
    session: AsyncSession,
    organization_id: uuid.UUID,
    settings: Settings,
    org_settings: dict[str, object],
) -> None:
    """Refuse new AI work once the organization has spent its monthly budget.

    Only effective when token prices are configured; without prices no cost is
    recorded and there is nothing to compare.
    """
    override = org_settings.get("monthly_llm_budget_usd")
    budget = Decimal(str(override)) if override is not None else settings.llm_monthly_budget_usd
    if budget is None:
        return
    month_start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    spent = await AgentLogRepository(session, organization_id).cost_since(month_start)
    if spent >= budget:
        raise RateLimitedError(
            "This organization has used its AI budget for the month.", code="budget_exceeded"
        )


async def record_llm_metrics(record: LLMCallRecord) -> None:
    """Counts, latency, tokens and cost of one call to the model provider."""
    labels = f'purpose="{record.purpose}",model="{record.model}"'
    samples = {
        f'qa_llm_calls_total{{{labels},status="{record.status}"}}': 1.0,
        f"qa_llm_latency_seconds_sum{{{labels}}}": record.latency_ms / 1000,
        f"qa_llm_latency_seconds_count{{{labels}}}": 1.0,
        f'qa_llm_tokens_total{{model="{record.model}",direction="input"}}': float(
            record.usage.input_tokens
        ),
        f'qa_llm_tokens_total{{model="{record.model}",direction="output"}}': float(
            record.usage.output_tokens
        ),
    }
    if record.usage.cost_usd is not None:
        samples[f'qa_llm_cost_usd_total{{model="{record.model}"}}'] = float(record.usage.cost_usd)
    await metrics.add(samples)
