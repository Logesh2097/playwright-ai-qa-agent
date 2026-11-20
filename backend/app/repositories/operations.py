"""MCP connections and the agent's tool-call trace."""

from collections.abc import Sequence

from sqlalchemy import select

from app.models import AgentExecutionLog, McpConnection
from app.repositories.base import TenantRepository

MANAGED_CONNECTION_NAME = "managed-playwright"


class McpConnectionRepository(TenantRepository[McpConnection]):
    model = McpConnection

    async def managed(self, *, for_update: bool = False) -> McpConnection | None:
        statement = self._select().where(McpConnection.name == MANAGED_CONNECTION_NAME)
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()


class AgentLogRepository(TenantRepository[AgentExecutionLog]):
    model = AgentExecutionLog

    async def for_thread(self, thread_id: str, *, limit: int = 200) -> Sequence[AgentExecutionLog]:
        result = await self._session.execute(
            select(AgentExecutionLog)
            .where(
                AgentExecutionLog.organization_id == self._organization_id,
                AgentExecutionLog.thread_id == thread_id,
            )
            .order_by(AgentExecutionLog.created_at, AgentExecutionLog.id)
            .limit(limit)
        )
        return result.scalars().all()
