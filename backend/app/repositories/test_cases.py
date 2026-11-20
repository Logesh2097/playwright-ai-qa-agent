"""Repositories for test cases, their versions and steps, and suites."""

import uuid
from collections.abc import Sequence

from sqlalchemy import delete, func, select, text, update

from app.models import TestCase, TestCaseVersion, TestStep, TestSuite, TestSuiteCase
from app.repositories.base import TenantRepository


class TestCaseRepository(TenantRepository[TestCase]):
    __test__ = False
    model = TestCase

    async def get_in_project(self, project_id: uuid.UUID, case_id: uuid.UUID) -> TestCase | None:
        result = await self._session.execute(
            self._select().where(TestCase.id == case_id, TestCase.project_id == project_id)
        )
        return result.scalar_one_or_none()

    async def list_in_project(
        self,
        project_id: uuid.UUID,
        *,
        instruction_id: uuid.UUID | None = None,
        status: str | None = None,
        category: str | None = None,
        ids: Sequence[uuid.UUID] | None = None,
        limit: int = 500,
    ) -> Sequence[TestCase]:
        statement = self._select().where(TestCase.project_id == project_id)
        if instruction_id is not None:
            statement = statement.where(TestCase.instruction_id == instruction_id)
        if status is not None:
            statement = statement.where(TestCase.status == status)
        if category is not None:
            statement = statement.where(TestCase.category == category)
        if ids is not None:
            statement = statement.where(TestCase.id.in_(ids))
        return await self._all(statement.order_by(TestCase.key).limit(limit))

    async def by_ids_any(self, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, TestCase]:
        """Cases by ID, including deleted ones: results keep pointing at them."""
        if not ids:
            return {}
        result = await self._session.execute(
            select(TestCase).where(
                TestCase.organization_id == self._organization_id, TestCase.id.in_(ids)
            )
        )
        return {case.id: case for case in result.scalars()}

    async def next_key_number(self, project_id: uuid.UUID) -> int:
        """Next free TC number in the project. Serialised per project for the transaction."""
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:project))"), {"project": str(project_id)}
        )
        # Deleted cases keep their keys, so they are counted too.
        result = await self._session.execute(
            select(func.count()).where(
                TestCase.organization_id == self._organization_id,
                TestCase.project_id == project_id,
            )
        )
        return int(result.scalar_one()) + 1


class CaseVersionRepository(TenantRepository[TestCaseVersion]):
    model = TestCaseVersion

    async def list_for_case(self, case_id: uuid.UUID) -> Sequence[TestCaseVersion]:
        return await self._all(
            self._select()
            .where(TestCaseVersion.test_case_id == case_id)
            .order_by(TestCaseVersion.version)
        )

    async def get_number(self, case_id: uuid.UUID, number: int) -> TestCaseVersion | None:
        result = await self._session.execute(
            self._select().where(
                TestCaseVersion.test_case_id == case_id, TestCaseVersion.version == number
            )
        )
        return result.scalar_one_or_none()

    async def by_ids(self, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, TestCaseVersion]:
        if not ids:
            return {}
        rows = await self._all(self._select().where(TestCaseVersion.id.in_(ids)))
        return {row.id: row for row in rows}

    async def automated_ids(self, version_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        """Which of these versions have an executable script."""
        if not version_ids:
            return set()
        result = await self._session.execute(
            select(TestCaseVersion.id).where(
                TestCaseVersion.organization_id == self._organization_id,
                TestCaseVersion.id.in_(version_ids),
                # A missing script may be stored as JSON null, which is not SQL NULL.
                func.jsonb_typeof(TestCaseVersion.automation) == "object",
            )
        )
        return set(result.scalars())

    async def steps(self, version_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, list[TestStep]]:
        grouped: dict[uuid.UUID, list[TestStep]] = {v: [] for v in version_ids}
        if not version_ids:
            return grouped
        result = await self._session.execute(
            select(TestStep)
            .where(
                TestStep.organization_id == self._organization_id,
                TestStep.test_case_version_id.in_(version_ids),
            )
            .order_by(TestStep.test_case_version_id, TestStep.position)
        )
        for step in result.scalars():
            grouped[step.test_case_version_id].append(step)
        return grouped


class SuiteRepository(TenantRepository[TestSuite]):
    model = TestSuite

    async def get_in_project(self, project_id: uuid.UUID, suite_id: uuid.UUID) -> TestSuite | None:
        result = await self._session.execute(
            self._select().where(TestSuite.id == suite_id, TestSuite.project_id == project_id)
        )
        return result.scalar_one_or_none()

    async def list_in_project(
        self, project_id: uuid.UUID, *, feature: str | None, status: str | None
    ) -> Sequence[TestSuite]:
        statement = self._select().where(TestSuite.project_id == project_id)
        if feature is not None:
            statement = statement.where(TestSuite.feature == feature)
        if status is not None:
            statement = statement.where(TestSuite.status == status)
        # Grouped by feature, most urgent first.
        return await self._all(
            statement.order_by(TestSuite.feature.nulls_last(), TestSuite.priority, TestSuite.name)
        )

    async def members(self, suite_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, list[TestSuiteCase]]:
        grouped: dict[uuid.UUID, list[TestSuiteCase]] = {s: [] for s in suite_ids}
        if not suite_ids:
            return grouped
        result = await self._session.execute(
            select(TestSuiteCase)
            .where(
                TestSuiteCase.organization_id == self._organization_id,
                TestSuiteCase.suite_id.in_(suite_ids),
            )
            .order_by(TestSuiteCase.suite_id, TestSuiteCase.position)
        )
        for member in result.scalars():
            grouped[member.suite_id].append(member)
        return grouped

    async def replace_members(self, suite_id: uuid.UUID, case_ids: Sequence[uuid.UUID]) -> None:
        await self._session.execute(
            delete(TestSuiteCase).where(
                TestSuiteCase.organization_id == self._organization_id,
                TestSuiteCase.suite_id == suite_id,
            )
        )
        self._session.add_all(
            TestSuiteCase(
                organization_id=self._organization_id,
                suite_id=suite_id,
                test_case_id=case_id,
                position=position,
            )
            for position, case_id in enumerate(case_ids, start=1)
        )
        await self._session.flush()

    async def suites_containing(self, case_id: uuid.UUID) -> Sequence[uuid.UUID]:
        result = await self._session.execute(
            select(TestSuiteCase.suite_id).where(
                TestSuiteCase.organization_id == self._organization_id,
                TestSuiteCase.test_case_id == case_id,
            )
        )
        return result.scalars().all()

    async def remove_case_everywhere(self, case_id: uuid.UUID) -> None:
        await self._session.execute(
            delete(TestSuiteCase).where(
                TestSuiteCase.organization_id == self._organization_id,
                TestSuiteCase.test_case_id == case_id,
            )
        )

    async def reopen(self, suite_ids: Sequence[uuid.UUID]) -> None:
        """An approved suite whose content changed is a draft again, with nothing pinned."""
        if not suite_ids:
            return
        await self._session.execute(
            update(TestSuite)
            .where(
                TestSuite.organization_id == self._organization_id,
                TestSuite.id.in_(suite_ids),
                TestSuite.status == "approved",
            )
            .values(status="draft", approved_by=None, approved_at=None)
        )
        await self._session.execute(
            update(TestSuiteCase)
            .where(
                TestSuiteCase.organization_id == self._organization_id,
                TestSuiteCase.suite_id.in_(suite_ids),
            )
            .values(approved_version_id=None)
        )
