"""Declarative base, shared column mixins and constraint helpers."""

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, MetaData, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.utils.ids import uuid7

# Explicit constraint names keep Alembic migrations deterministic.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

EMPTY_JSON_OBJECT = text("'{}'::jsonb")
EMPTY_JSON_ARRAY = text("'[]'::jsonb")
EMPTY_TEXT_ARRAY = text("'{}'::text[]")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
        datetime: DateTime(timezone=True),
    }
    # Several models are named Test*; keep pytest from trying to collect them.
    __test__ = False


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)


def utcnow() -> datetime:
    return datetime.now(UTC)


# Timestamps are set by the application as well as by column defaults. Supplying
# them in the INSERT means SQLAlchemy has no reason to add RETURNING, which under
# row-level security would require the new row to be readable (audit rows are not).
class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), default=utcnow, onupdate=utcnow
    )


class CreatedAtMixin:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), default=utcnow)


class SoftDeleteMixin:
    deleted_at: Mapped[datetime | None] = mapped_column(default=None)


class TenantMixin:
    """Marks a table as tenant-owned. Row-level security is applied to every such table."""

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), index=True
    )


def one_of(column: str, values: Iterable[str]) -> CheckConstraint:
    """CHECK constraint restricting a text column to a fixed set of values."""
    quoted = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({quoted})", name=f"{column}_valid")
