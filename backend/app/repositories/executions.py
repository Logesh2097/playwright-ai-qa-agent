"""Executions, their results, steps and evidence."""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select

from app.models import (
    LocatorHealProposal,
    TestEvidence,
    TestExecution,
    TestExecutionStep,
    TestResult,
)
from app.models.execution import ACTIVE_EXECUTION_STATUSES
from app.repositories.base import TenantRepository


class ExecutionRepository(TenantRepository[TestExecution]):
    model = TestExecution

    async def get_in_project(
        self, project_id: uuid.UUID, execution_id: uuid.UUID, *, for_update: bool = False
    ) -> TestExecution | None:
        statement = self._select().where(
            TestExecution.project_id == project_id, TestExecution.id == execution_id
        )
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_for_update(self, execution_id: uuid.UUID) -> TestExecution | None:
        result = await self._session.execute(
            self._select().where(TestExecution.id == execution_id).with_for_update()
        )
        return result.scalar_one_or_none()

    async def list_in_project(
        self, project_id: uuid.UUID, *, limit: int = 50
    ) -> Sequence[TestExecution]:
        return await self._all(
            self._select()
            .where(TestExecution.project_id == project_id)
            .order_by(TestExecution.created_at.desc(), TestExecution.id.desc())
            .limit(limit)
        )

    async def get_by_idempotency_key(self, key: str) -> TestExecution | None:
        result = await self._session.execute(
            self._select().where(TestExecution.idempotency_key == key)
        )
        return result.scalar_one_or_none()

    async def for_commit(
        self, project_id: uuid.UUID, repository_id: int, head_sha: str
    ) -> Sequence[TestExecution]:
        """Executions started for one commit of one linked repository, oldest first."""
        return await self._all(
            self._select()
            .where(
                TestExecution.project_id == project_id,
                TestExecution.git_context["repository_id"].as_integer() == repository_id,
                TestExecution.git_context["head_sha"].as_string() == head_sha,
            )
            .order_by(TestExecution.created_at, TestExecution.id)
        )

    async def last_comment_id(
        self, project_id: uuid.UUID, repository_id: int, pull_request: int
    ) -> int | None:
        """The pull request comment an earlier execution wrote, to update it in place."""
        result = await self._session.execute(
            self._select()
            .where(
                TestExecution.project_id == project_id,
                TestExecution.git_context["repository_id"].as_integer() == repository_id,
                TestExecution.git_context["pull_request"].as_integer() == pull_request,
                TestExecution.git_context["comment_id"].as_integer().is_not(None),
            )
            .order_by(TestExecution.created_at.desc())
            .limit(1)
        )
        found = result.scalar_one_or_none()
        value = (found.git_context or {}).get("comment_id") if found else None
        return int(value) if value is not None else None

    async def search(
        self,
        project_id: uuid.UUID,
        *,
        status: str | None = None,
        verdict: str | None = None,
        environment_id: uuid.UUID | None = None,
        suite_id: uuid.UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[TestExecution], int]:
        """A page of the project's executions, newest first, and how many match in all."""
        conditions = [TestExecution.project_id == project_id]
        if status is not None:
            conditions.append(TestExecution.status == status)
        if verdict is not None:
            conditions.append(TestExecution.verdict == verdict)
        if environment_id is not None:
            conditions.append(TestExecution.environment_id == environment_id)
        if suite_id is not None:
            conditions.append(TestExecution.suite_id == suite_id)
        if since is not None:
            conditions.append(TestExecution.created_at >= since)
        if until is not None:
            conditions.append(TestExecution.created_at <= until)
        rows = await self._all(
            self._select()
            .where(*conditions)
            .order_by(TestExecution.created_at.desc(), TestExecution.id.desc())
            .limit(limit)
            .offset(offset)
        )
        total = await self._session.execute(
            select(func.count())
            .select_from(TestExecution)
            .where(TestExecution.organization_id == self._organization_id, *conditions)
        )
        return rows, int(total.scalar_one())

    async def count_active(self) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(TestExecution)
            .where(
                TestExecution.organization_id == self._organization_id,
                TestExecution.status.in_((*ACTIVE_EXECUTION_STATUSES, "cancelling")),
            )
        )
        return int(result.scalar_one())


class ResultRepository(TenantRepository[TestResult]):
    model = TestResult

    async def for_execution(self, execution_id: uuid.UUID) -> Sequence[TestResult]:
        return await self._all(
            self._select()
            .where(TestResult.execution_id == execution_id)
            .order_by(TestResult.created_at, TestResult.id)
        )

    async def history(
        self,
        project_id: uuid.UUID,
        *,
        case_ids: Sequence[uuid.UUID] | None = None,
        statuses: Sequence[str] | None = None,
        since: datetime | None = None,
        limit: int = 5000,
    ) -> Sequence[tuple[TestResult, uuid.UUID]]:
        """Finished results in a project with their environment, newest first."""
        statement = (
            select(TestResult, TestExecution.environment_id)
            .join(TestExecution, TestExecution.id == TestResult.execution_id)
            .where(
                TestResult.organization_id == self._organization_id,
                TestExecution.organization_id == self._organization_id,
                TestExecution.project_id == project_id,
                TestResult.finished_at.is_not(None),
            )
            .order_by(TestResult.finished_at.desc(), TestResult.created_at.desc())
            .limit(limit)
        )
        if case_ids is not None:
            statement = statement.where(TestResult.test_case_id.in_(case_ids))
        if statuses is not None:
            statement = statement.where(TestResult.status.in_(statuses))
        if since is not None:
            # By when the execution was started, like every other window in the reports.
            statement = statement.where(TestExecution.created_at >= since)
        rows = await self._session.execute(statement)
        return [(row[0], row[1]) for row in rows.all()]

    async def get_in_execution(
        self, execution_id: uuid.UUID, result_id: uuid.UUID
    ) -> TestResult | None:
        result = await self._session.execute(
            self._select().where(
                TestResult.execution_id == execution_id, TestResult.id == result_id
            )
        )
        return result.scalar_one_or_none()


class StepRepository(TenantRepository[TestExecutionStep]):
    model = TestExecutionStep

    async def for_result(self, result_id: uuid.UUID) -> Sequence[TestExecutionStep]:
        return await self._all(
            self._select()
            .where(TestExecutionStep.result_id == result_id)
            .order_by(TestExecutionStep.attempt, TestExecutionStep.position)
        )

    async def for_execution(self, execution_id: uuid.UUID) -> Sequence[TestExecutionStep]:
        return await self._all(
            self._select()
            .where(TestExecutionStep.execution_id == execution_id)
            .order_by(
                TestExecutionStep.result_id, TestExecutionStep.attempt, TestExecutionStep.position
            )
        )


class EvidenceRepository(TenantRepository[TestEvidence]):
    model = TestEvidence

    async def for_execution(self, execution_id: uuid.UUID) -> Sequence[TestEvidence]:
        return await self._all(
            self._select()
            .where(TestEvidence.execution_id == execution_id)
            .order_by(TestEvidence.created_at, TestEvidence.id)
        )

    async def get_in_execution(
        self, execution_id: uuid.UUID, evidence_id: uuid.UUID
    ) -> TestEvidence | None:
        result = await self._session.execute(
            self._select().where(
                TestEvidence.execution_id == execution_id, TestEvidence.id == evidence_id
            )
        )
        return result.scalar_one_or_none()


class HealProposalRepository(TenantRepository[LocatorHealProposal]):
    model = LocatorHealProposal

    async def open_duplicate(
        self,
        *,
        version_id: uuid.UUID,
        phase: str,
        script_index: int | None,
        new_locator: dict[str, Any],
    ) -> bool:
        """Whether the same replacement for the same step is already waiting for a decision."""
        rows = await self._all(
            self._select().where(
                LocatorHealProposal.test_case_version_id == version_id,
                LocatorHealProposal.script_phase == phase,
                LocatorHealProposal.script_index == script_index,
                LocatorHealProposal.status.in_(("proposed", "verified")),
            )
        )
        return any(row.new_locator == new_locator for row in rows)

    async def list_for_cases(
        self, case_ids: Sequence[uuid.UUID], *, status: str | None = None
    ) -> Sequence[LocatorHealProposal]:
        if not case_ids:
            return []
        statement = self._select().where(LocatorHealProposal.test_case_id.in_(case_ids))
        if status is not None:
            statement = statement.where(LocatorHealProposal.status == status)
        return await self._all(statement.order_by(LocatorHealProposal.created_at.desc()))

    async def get_for_update(self, proposal_id: uuid.UUID) -> LocatorHealProposal | None:
        result = await self._session.execute(
            self._select().where(LocatorHealProposal.id == proposal_id).with_for_update()
        )
        return result.scalar_one_or_none()
