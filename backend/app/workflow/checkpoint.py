"""LangGraph checkpoints in PostgreSQL, through the application's own database layer.

Written against `BaseCheckpointSaver` of langgraph-checkpoint 4.x. The official
PostgreSQL saver is not used because it needs a second database driver and its
tables would sit outside row-level security. Here every row carries the
organization and every transaction sets the tenant context first, like any
other tenant data.

One saver serves one organization. Serialization is LangGraph's own
(`self.serde`): the saver stores the bytes it is given and returns them unchanged.
"""

import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.context import set_current_org
from app.models import WorkflowCheckpoint, WorkflowCheckpointWrite


class PostgresCheckpointSaver(BaseCheckpointSaver[str]):
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], organization_id: uuid.UUID
    ) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._organization_id = organization_id

    # --- reading -------------------------------------------------------------------------

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id = str(config["configurable"]["thread_id"])
        namespace = str(config["configurable"].get("checkpoint_ns", ""))
        checkpoint_id = get_checkpoint_id(config)
        async with self._session_factory() as session, session.begin():
            await set_current_org(session, self._organization_id)
            statement = select(WorkflowCheckpoint).where(
                WorkflowCheckpoint.organization_id == self._organization_id,
                WorkflowCheckpoint.thread_id == thread_id,
                WorkflowCheckpoint.checkpoint_ns == namespace,
            )
            if checkpoint_id:
                statement = statement.where(WorkflowCheckpoint.checkpoint_id == checkpoint_id)
            else:
                # Checkpoint IDs are time-ordered, so the greatest is the latest.
                statement = statement.order_by(WorkflowCheckpoint.checkpoint_id.desc()).limit(1)
            row = (await session.execute(statement)).scalar_one_or_none()
            if row is None:
                return None
            return await self._tuple(session, row)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        async with self._session_factory() as session, session.begin():
            await set_current_org(session, self._organization_id)
            statement = select(WorkflowCheckpoint).where(
                WorkflowCheckpoint.organization_id == self._organization_id
            )
            if config is not None:
                statement = statement.where(
                    WorkflowCheckpoint.thread_id == str(config["configurable"]["thread_id"]),
                    WorkflowCheckpoint.checkpoint_ns
                    == str(config["configurable"].get("checkpoint_ns", "")),
                )
            if before is not None and (before_id := get_checkpoint_id(before)):
                statement = statement.where(WorkflowCheckpoint.checkpoint_id < before_id)
            statement = statement.order_by(WorkflowCheckpoint.checkpoint_id.desc())
            rows = (await session.execute(statement)).scalars().all()
            tuples = [await self._tuple(session, row) for row in rows]
        yielded = 0
        for item in tuples:
            if filter and any(item.metadata.get(k) != v for k, v in filter.items()):
                continue
            if limit is not None and yielded >= limit:
                return
            yielded += 1
            yield item

    async def _tuple(self, session: AsyncSession, row: WorkflowCheckpoint) -> CheckpointTuple:
        writes = (
            (
                await session.execute(
                    select(WorkflowCheckpointWrite)
                    .where(
                        WorkflowCheckpointWrite.organization_id == self._organization_id,
                        WorkflowCheckpointWrite.thread_id == row.thread_id,
                        WorkflowCheckpointWrite.checkpoint_ns == row.checkpoint_ns,
                        WorkflowCheckpointWrite.checkpoint_id == row.checkpoint_id,
                    )
                    .order_by(
                        WorkflowCheckpointWrite.task_path,
                        WorkflowCheckpointWrite.task_id,
                        WorkflowCheckpointWrite.idx,
                    )
                )
            )
            .scalars()
            .all()
        )
        configurable = {"thread_id": row.thread_id, "checkpoint_ns": row.checkpoint_ns}
        return CheckpointTuple(
            config={"configurable": {**configurable, "checkpoint_id": row.checkpoint_id}},
            checkpoint=self.serde.loads_typed((row.checkpoint_type, row.checkpoint)),
            metadata=self.serde.loads_typed((row.metadata_type, row.checkpoint_metadata)),
            parent_config=(
                {"configurable": {**configurable, "checkpoint_id": row.parent_checkpoint_id}}
                if row.parent_checkpoint_id
                else None
            ),
            pending_writes=[
                (w.task_id, w.channel, self.serde.loads_typed((w.value_type, w.value)))
                for w in writes
            ],
        )

    # --- writing -------------------------------------------------------------------------

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id = str(config["configurable"]["thread_id"])
        namespace = str(config["configurable"].get("checkpoint_ns", ""))
        checkpoint_type, checkpoint_bytes = self.serde.dumps_typed(checkpoint)
        metadata_type, metadata_bytes = self.serde.dumps_typed(
            get_checkpoint_metadata(config, metadata)
        )
        values = {
            "organization_id": self._organization_id,
            "thread_id": thread_id,
            "checkpoint_ns": namespace,
            "checkpoint_id": checkpoint["id"],
            "parent_checkpoint_id": config["configurable"].get("checkpoint_id"),
            "checkpoint_type": checkpoint_type,
            "checkpoint": checkpoint_bytes,
            "metadata_type": metadata_type,
            "checkpoint_metadata": metadata_bytes,
        }
        async with self._session_factory() as session, session.begin():
            await set_current_org(session, self._organization_id)
            await session.execute(
                insert(WorkflowCheckpoint)
                .values(id=uuid.uuid4(), **values)
                .on_conflict_do_update(
                    index_elements=["thread_id", "checkpoint_ns", "checkpoint_id"],
                    set_={
                        k: values[k]
                        for k in (
                            "checkpoint_type",
                            "checkpoint",
                            "metadata_type",
                            "checkpoint_metadata",
                        )
                    },
                )
            )
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": namespace,
                "checkpoint_id": checkpoint["id"],
            }
        }

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        base = {
            "organization_id": self._organization_id,
            "thread_id": str(config["configurable"]["thread_id"]),
            "checkpoint_ns": str(config["configurable"].get("checkpoint_ns", "")),
            "checkpoint_id": str(config["configurable"]["checkpoint_id"]),
            "task_id": task_id,
            "task_path": task_path,
        }
        key = ["thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx"]
        async with self._session_factory() as session, session.begin():
            await set_current_org(session, self._organization_id)
            for position, (channel, value) in enumerate(writes):
                idx = WRITES_IDX_MAP.get(channel, position)
                value_type, value_bytes = self.serde.dumps_typed(value)
                statement = insert(WorkflowCheckpointWrite).values(
                    id=uuid.uuid4(),
                    idx=idx,
                    channel=channel,
                    value_type=value_type,
                    value=value_bytes,
                    **base,
                )
                if idx >= 0:
                    # An ordinary write that is already stored stays as it is.
                    statement = statement.on_conflict_do_nothing(index_elements=key)
                else:
                    # Special writes (errors, interrupts, resume values) replace earlier ones.
                    statement = statement.on_conflict_do_update(
                        index_elements=key,
                        set_={"channel": channel, "value_type": value_type, "value": value_bytes},
                    )
                await session.execute(statement)

    async def adelete_thread(self, thread_id: str) -> None:
        async with self._session_factory() as session, session.begin():
            await set_current_org(session, self._organization_id)
            for model in (WorkflowCheckpointWrite, WorkflowCheckpoint):
                await session.execute(
                    delete(model).where(
                        model.organization_id == self._organization_id,
                        model.thread_id == str(thread_id),
                    )
                )

    # --- the synchronous interface is not used: the worker is asynchronous ----------------

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        raise NotImplementedError("use the asynchronous methods")

    def list(self, config: RunnableConfig | None, **kwargs: Any) -> Iterator[CheckpointTuple]:
        raise NotImplementedError("use the asynchronous methods")

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        raise NotImplementedError("use the asynchronous methods")

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        raise NotImplementedError("use the asynchronous methods")
