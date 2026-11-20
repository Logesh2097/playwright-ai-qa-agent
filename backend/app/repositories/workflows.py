"""Workflow runs, approvals and the workflow's trace."""

import uuid
from collections.abc import Sequence

from sqlalchemy import func, or_, select

from app.models import AgentExecutionLog, Approval, WorkflowRun
from app.models.workflow import ACTIVE_WORKFLOW_STATUSES, WAITING_WORKFLOW_STATUSES
from app.repositories.base import TenantRepository


class WorkflowRunRepository(TenantRepository[WorkflowRun]):
    model = WorkflowRun

    async def get_in_project(
        self, project_id: uuid.UUID, run_id: uuid.UUID, *, for_update: bool = False
    ) -> WorkflowRun | None:
        statement = self._select().where(
            WorkflowRun.project_id == project_id, WorkflowRun.id == run_id
        )
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_for_update(self, run_id: uuid.UUID) -> WorkflowRun | None:
        result = await self._session.execute(
            self._select().where(WorkflowRun.id == run_id).with_for_update()
        )
        return result.scalar_one_or_none()

    async def list_in_project(
        self, project_id: uuid.UUID, *, limit: int = 50
    ) -> Sequence[WorkflowRun]:
        return await self._all(
            self._select()
            .where(WorkflowRun.project_id == project_id)
            .order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc())
            .limit(limit)
        )

    async def count_open(self) -> int:
        """Runs that are working or waiting for a person."""
        result = await self._session.execute(
            select(func.count())
            .select_from(WorkflowRun)
            .where(
                WorkflowRun.organization_id == self._organization_id,
                WorkflowRun.status.in_((*ACTIVE_WORKFLOW_STATUSES, *WAITING_WORKFLOW_STATUSES)),
            )
        )
        return int(result.scalar_one())


class ApprovalRepository(TenantRepository[Approval]):
    model = Approval

    async def latest_for(self, subject_id: uuid.UUID) -> Approval | None:
        result = await self._session.execute(
            self._select()
            .where(Approval.subject_id == subject_id)
            .order_by(Approval.created_at.desc(), Approval.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


class WorkflowLogRepository(TenantRepository[AgentExecutionLog]):
    model = AgentExecutionLog

    async def for_run(
        self, run_id: uuid.UUID, instruction_id: uuid.UUID | None, *, limit: int = 1000
    ) -> Sequence[AgentExecutionLog]:
        """Everything recorded for a run: its own events, and the model calls of its instruction."""
        owned = AgentExecutionLog.thread_id == str(run_id)
        condition = (
            or_(owned, AgentExecutionLog.instruction_id == instruction_id)
            if instruction_id is not None
            else owned
        )
        return await self._all(
            self._select()
            .where(condition)
            .order_by(AgentExecutionLog.created_at, AgentExecutionLog.id)
            .limit(limit)
        )
