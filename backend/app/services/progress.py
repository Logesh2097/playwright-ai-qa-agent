"""Live progress of an execution, as the worker sees it.

The worker writes what the engine reports, as it reports it, to Redis: which
case is running, each step as it finishes. The API reads it for the live view.
Nothing here is estimated or made up: an entry exists only for something the
engine said happened. Results themselves are stored in the database when a
case ends; this is only for watching.

Entries passed the engine's redactor before they arrive here. Values typed,
expected and found are left out of the live view; they are in the report.
"""

import contextlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from app.core.redis import RedisClient
from app.engine.protocol import CaseFinished, CaseStarted, StepFinished

TTL_S = 6 * 3600
MAX_EVENTS = 400


def current_key(execution_id: uuid.UUID | str) -> str:
    return f"qa:execution:{execution_id}:current"


def events_key(execution_id: uuid.UUID | str) -> str:
    return f"qa:execution:{execution_id}:events"


def counter_key(execution_id: uuid.UUID | str) -> str:
    return f"qa:execution:{execution_id}:seq"


class ProgressWriter:
    """Used by the worker. A failure to write never disturbs the run."""

    def __init__(self, redis: RedisClient, execution_id: uuid.UUID) -> None:
        self._redis = redis
        self._id = execution_id

    async def _append(self, entry: dict[str, Any], current: dict[str, Any] | None) -> None:
        try:
            sequence = int(await self._redis.incr(counter_key(self._id)))
            entry = {"seq": sequence, "at": datetime.now(UTC).isoformat(), **entry}
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.rpush(events_key(self._id), json.dumps(entry))
                pipe.ltrim(events_key(self._id), -MAX_EVENTS, -1)
                pipe.expire(events_key(self._id), TTL_S)
                pipe.expire(counter_key(self._id), TTL_S)
                if current is not None:
                    pipe.set(current_key(self._id), json.dumps(current), ex=TTL_S)
                await pipe.execute()
        except Exception:  # noqa: S110 - watching is optional; the run is not
            pass

    async def case_started(self, event: CaseStarted) -> None:
        await self._append(
            {"type": "case_started", "result_id": event.result_id, "attempt": event.attempt},
            {"result_id": event.result_id, "attempt": event.attempt, "step": None},
        )

    async def step_finished(self, event: StepFinished) -> None:
        step = {
            "position": event.position,
            "phase": event.phase,
            "action": event.action,
            "description": event.description,
            "status": event.status,
            "duration_ms": event.duration_ms,
            "page_url": event.page_url,
            "error_type": event.error_type,
            "error_message": event.error_message,
        }
        await self._append(
            {
                "type": "step_finished",
                "result_id": event.result_id,
                "attempt": event.attempt,
                **step,
            },
            {"result_id": event.result_id, "attempt": event.attempt, "step": step},
        )

    async def case_finished(self, event: CaseFinished) -> None:
        await self._append(
            {
                "type": "case_finished",
                "result_id": event.result_id,
                "attempt": event.attempt,
                "status": event.status,
                "final": event.final,
            },
            None,
        )

    async def ended(self) -> None:
        with contextlib.suppress(Exception):
            await self._redis.delete(current_key(self._id))


async def read_progress(
    redis: RedisClient, execution_id: uuid.UUID, after: int
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """The step in progress, and the entries with a sequence number above `after`."""
    raw_current = await redis.get(current_key(execution_id))
    raw_events: Any = await redis.lrange(events_key(execution_id), 0, -1)  # type: ignore[misc]
    events = [json.loads(item) for item in raw_events]
    current = json.loads(raw_current) if raw_current else None
    return current, [entry for entry in events if int(entry.get("seq", 0)) > after]
