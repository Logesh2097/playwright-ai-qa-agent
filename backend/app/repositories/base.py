"""Repository base classes.

Repositories are the only layer that issues queries. They never commit: the
unit of work (one transaction per request or job) belongs to the caller.

`TenantRepository` is the first of two isolation layers. Every query it builds
is filtered by the organization it was constructed with; PostgreSQL row-level
security is the second layer and catches anything that bypasses a repository.
"""

import uuid
from collections.abc import Sequence
from typing import Any, ClassVar

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import Base


class BaseRepository[ModelT: Base]:
    model: ClassVar[type[Any]]

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _select(self) -> Select[ModelT]:
        return select(self.model)

    async def get(self, entity_id: uuid.UUID) -> ModelT | None:
        result = await self._session.execute(self._select().where(self.model.id == entity_id))
        return result.scalar_one_or_none()

    async def add(self, entity: ModelT) -> ModelT:
        """Insert now (flush), so constraint violations surface at the call site."""
        self._session.add(entity)
        await self._session.flush()
        return entity

    async def _all(self, statement: Select[ModelT]) -> Sequence[ModelT]:
        return (await self._session.execute(statement)).scalars().all()


class TenantRepository[ModelT: Base](BaseRepository[ModelT]):
    """Repository for a table that has an `organization_id` column."""

    def __init__(self, session: AsyncSession, organization_id: uuid.UUID) -> None:
        super().__init__(session)
        self._organization_id = organization_id

    def _select(self) -> Select[ModelT]:
        statement: Select[ModelT] = select(self.model).where(
            self.model.organization_id == self._organization_id
        )
        if hasattr(self.model, "deleted_at"):
            statement = statement.where(self.model.deleted_at.is_(None))
        return statement

    async def add(self, entity: ModelT) -> ModelT:
        if getattr(entity, "organization_id", None) != self._organization_id:
            raise ValueError("entity belongs to a different organization than this repository")
        return await super().add(entity)

    async def list(self, *, limit: int = 100) -> Sequence[ModelT]:
        return await self._all(self._select().order_by(self.model.id).limit(limit))
