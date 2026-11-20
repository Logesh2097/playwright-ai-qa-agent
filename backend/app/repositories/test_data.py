"""Test data sets and test files."""

import uuid
from collections.abc import Sequence

from sqlalchemy import delete
from sqlalchemy.orm import undefer

from app.models import TestDataSet, TestFile
from app.repositories.base import TenantRepository


class DataSetRepository(TenantRepository[TestDataSet]):
    model = TestDataSet

    async def get_in_project(self, project_id: uuid.UUID, set_id: uuid.UUID) -> TestDataSet | None:
        result = await self._session.execute(
            self._select().where(TestDataSet.project_id == project_id, TestDataSet.id == set_id)
        )
        return result.scalar_one_or_none()

    async def list_in_project(self, project_id: uuid.UUID) -> Sequence[TestDataSet]:
        return await self._all(
            self._select()
            .where(TestDataSet.project_id == project_id)
            .order_by(TestDataSet.name, TestDataSet.environment_id.is_not(None))
        )

    async def remove(self, data_set: TestDataSet) -> None:
        await self._session.execute(delete(TestDataSet).where(TestDataSet.id == data_set.id))


class TestFileRepository(TenantRepository[TestFile]):
    __test__ = False
    model = TestFile

    async def get_in_project(self, project_id: uuid.UUID, file_id: uuid.UUID) -> TestFile | None:
        result = await self._session.execute(
            self._select().where(TestFile.project_id == project_id, TestFile.id == file_id)
        )
        return result.scalar_one_or_none()

    async def list_in_project(self, project_id: uuid.UUID) -> Sequence[TestFile]:
        return await self._all(
            self._select().where(TestFile.project_id == project_id).order_by(TestFile.name)
        )

    async def with_content(self, project_id: uuid.UUID, names: Sequence[str]) -> Sequence[TestFile]:
        if not names:
            return []
        return await self._all(
            self._select()
            .where(TestFile.project_id == project_id, TestFile.name.in_(names))
            .options(undefer(TestFile.content))
        )

    async def remove(self, file: TestFile) -> None:
        await self._session.execute(delete(TestFile).where(TestFile.id == file.id))
