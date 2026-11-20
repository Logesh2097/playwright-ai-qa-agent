"""Background job queue.

Code enqueues through the `JobQueue` protocol, so the queue library (arq on
Redis) is a detail of this module and of `app/worker`.
"""

from typing import Any, Protocol

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings


class JobQueue(Protocol):
    async def enqueue(self, job: str, **kwargs: Any) -> None: ...


class ArqJobQueue:
    """Connects on first use, so creating it performs no I/O."""

    def __init__(self, redis_url: str) -> None:
        self._settings = RedisSettings.from_dsn(redis_url)
        self._pool: ArqRedis | None = None

    async def enqueue(self, job: str, **kwargs: Any) -> None:
        if self._pool is None:
            self._pool = await create_pool(self._settings)
        await self._pool.enqueue_job(job, **kwargs)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.aclose()
            self._pool = None
