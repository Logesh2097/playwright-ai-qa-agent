"""Repositories for test instructions, plans and the agent's usage log."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select, update

from app.models import AgentExecutionLog, TestInstruction, TestPlan
from app.repositories.base import TenantRepository


class InstructionRepository(TenantRepository[TestInstruction]):
    model = TestInstruction

    async def get_in_project(
        self, project_id: uuid.UUID, instruction_id: uuid.UUID
    ) -> TestInstruction | None:
        result = await self._session.execute(
            self._select().where(
                TestInstruction.id == instruction_id, TestInstruction.project_id == project_id
            )
        )
        return result.scalar_one_or_none()

    async def list_in_project(
        self, project_id: uuid.UUID, *, status: str | None, limit: int
    ) -> Sequence[TestInstruction]:
        statement = self._select().where(TestInstruction.project_id == project_id)
        if status is not None:
            statement = statement.where(TestInstruction.status == status)
        return await self._all(statement.order_by(TestInstruction.id.desc()).limit(limit))

    async def get_for_update(self, instruction_id: uuid.UUID) -> TestInstruction | None:
        result = await self._session.execute(
            self._select().where(TestInstruction.id == instruction_id).with_for_update()
        )
        return result.scalar_one_or_none()


class PlanRepository(TenantRepository[TestPlan]):
    model = TestPlan

    async def latest(self, instruction_id: uuid.UUID) -> TestPlan | None:
        result = await self._session.execute(
            self._select()
            .where(TestPlan.instruction_id == instruction_id)
            .order_by(TestPlan.version.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def supersede_all(self, instruction_id: uuid.UUID) -> None:
        await self._session.execute(
            update(TestPlan)
            .where(
                TestPlan.organization_id == self._organization_id,
                TestPlan.instruction_id == instruction_id,
                TestPlan.status != "superseded",
            )
            .values(status="superseded")
        )


@dataclass(frozen=True, slots=True)
class UsageTotals:
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal | None


class AgentLogRepository(TenantRepository[AgentExecutionLog]):
    model = AgentExecutionLog

    async def usage_for_instruction(self, instruction_id: uuid.UUID) -> UsageTotals:
        row = (
            await self._session.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(AgentExecutionLog.prompt_tokens), 0),
                    func.coalesce(func.sum(AgentExecutionLog.completion_tokens), 0),
                    func.sum(AgentExecutionLog.cost_usd),
                ).where(
                    AgentExecutionLog.organization_id == self._organization_id,
                    AgentExecutionLog.instruction_id == instruction_id,
                    AgentExecutionLog.event_type == "llm_call",
                )
            )
        ).one()
        return UsageTotals(int(row[0] or 0), int(row[1] or 0), int(row[2] or 0), row[3])

    async def cost_since(self, since: datetime) -> Decimal:
        result = await self._session.execute(
            select(func.coalesce(func.sum(AgentExecutionLog.cost_usd), 0)).where(
                AgentExecutionLog.organization_id == self._organization_id,
                AgentExecutionLog.created_at >= since,
            )
        )
        return Decimal(result.scalar_one() or 0)
