"""Running a workflow in the worker: start it, resume it, pick it up after an interruption.

One call runs the graph until it ends or has to wait for a person. LangGraph
saves a checkpoint after every node, so the next call (with the person's answer,
or simply "carry on") continues from where the last one stopped.
"""

import asyncio
import contextlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from langgraph.types import Command

from app.models import WorkflowRun
from app.models.workflow import ENDED_WORKFLOW_STATUSES
from app.workflow.checkpoint import PostgresCheckpointSaver
from app.workflow.deps import WorkflowDeps
from app.workflow.graph import compile_graph
from app.workflow.observability import RunLog
from app.workflow.state import Options, initial_state

logger = logging.getLogger(__name__)

JOB_NAME = "run_workflow"
# Enough for the longest path: two clarification rounds, regeneration, retries.
RECURSION_LIMIT = 80
LOCK_TTL_S = 30
_WAITING = {"clarification": "waiting_for_clarification", "approval": "waiting_for_approval"}


# Runs a worker is working on, as "organization:run". A run that is listed here
# while nobody holds its lock was interrupted.
ACTIVE_SET = "workflow:active"
MAX_RECOVERIES = 3


def lock_key(run_id: uuid.UUID) -> str:
    return f"workflow:lock:{run_id}"


def recoveries_key(run_id: uuid.UUID) -> str:
    return f"workflow:recoveries:{run_id}"


class WorkflowRunner:
    def __init__(self, deps: WorkflowDeps) -> None:
        self._deps = deps

    async def run(
        self, *, organization_id: uuid.UUID, run_id: uuid.UUID, resume: dict[str, Any] | None = None
    ) -> str:
        """Returns the run's status when this call stops working on it."""
        # Typed loosely: the client's set commands are declared as "sync or async".
        redis: Any = self._deps.redis
        token = uuid.uuid4().hex
        # One worker at a time per run. The lock is renewed while the graph runs, so it
        # disappears soon after a worker dies and the run can then be picked up again.
        if not await redis.set(lock_key(run_id), token, nx=True, ex=LOCK_TTL_S):
            return "busy"
        member = f"{organization_id}:{run_id}"
        await redis.sadd(ACTIVE_SET, member)
        keep_alive = asyncio.create_task(self._renew(run_id, token))
        try:
            status = await self._invoke(organization_id, run_id, resume)
            # Ended, or waiting for a person: either way no worker is needed for now.
            await redis.srem(ACTIVE_SET, member)
            return status
        finally:
            # If the worker dies, none of this runs: the run stays listed as active and
            # its lock expires, which is how `recover_interrupted` recognises it.
            keep_alive.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keep_alive
            if await redis.get(lock_key(run_id)) in (token, token.encode()):
                await redis.delete(lock_key(run_id))

    async def _renew(self, run_id: uuid.UUID, token: str) -> None:
        while True:
            await asyncio.sleep(LOCK_TTL_S / 3)
            await self._deps.redis.set(lock_key(run_id), token, xx=True, ex=LOCK_TTL_S)

    async def _invoke(
        self, organization_id: uuid.UUID, run_id: uuid.UUID, resume: dict[str, Any] | None
    ) -> str:
        deps = self._deps
        async with deps.transaction(str(organization_id)) as session:
            run = await session.get(WorkflowRun, run_id)
            if run is None or run.status in ENDED_WORKFLOW_STATUSES:
                return "discarded"
            if run.started_at is None:
                run.started_at = datetime.now(UTC)
            first = initial_state(
                run_id=str(run.id),
                mode=run.mode,  # type: ignore[arg-type]
                organization_id=str(organization_id),
                project_id=str(run.project_id),
                environment_id=str(run.environment_id),
                user_id=str(run.started_by),
                instruction_id=str(run.instruction_id) if run.instruction_id else None,
                suite_id=str(run.suite_id) if run.suite_id else None,
                options=Options(**run.options),  # type: ignore[typeddict-item]
            )
            instruction_id = run.instruction_id

        graph = compile_graph(deps, PostgresCheckpointSaver(deps.session_factory, organization_id))
        config: Any = {
            "configurable": {"thread_id": str(run_id)},
            "recursion_limit": RECURSION_LIMIT,
        }
        log = RunLog(
            deps.session_factory,
            organization_id=organization_id,
            run_id=run_id,
            instruction_id=instruction_id,
        )
        snapshot = await graph.aget_state(config)
        started = bool(snapshot.values)
        graph_input: Any
        if resume is not None and started:
            # A person answered: hand the answer to the node that was waiting.
            await log.write(
                "resume",
                str(resume.get("type", "resume")),
                detail={"by": resume.get("decided_by") or resume.get("answered_by")},
            )
            graph_input = Command(resume=resume)
        elif started:
            # Nothing new to say: continue from the last checkpoint. This is how a run
            # interrupted by a worker stopping is picked up again.
            await log.write("resume", "recovery", detail={"next": list(snapshot.next)})
            graph_input = None
        else:
            graph_input = first
        try:
            result = await graph.ainvoke(graph_input, config)
        except Exception:
            logger.exception("workflow_failed", extra={"run_id": str(run_id)})
            await self._mark(
                organization_id,
                run_id,
                status="failed",
                error={"code": "internal_error", "message": "The workflow stopped unexpectedly."},
                finished=True,
            )
            return "failed"

        interrupts = result.get("__interrupt__") or []
        if interrupts:
            waiting_for = dict(interrupts[0].value)
            status = _WAITING.get(str(waiting_for.get("type")), "waiting_for_approval")
            await self._mark(organization_id, run_id, status=status, pending=waiting_for)
            return status
        async with deps.transaction(str(organization_id)) as session:
            run = await session.get(WorkflowRun, run_id)
            return run.status if run else "discarded"

    async def _mark(
        self,
        organization_id: uuid.UUID,
        run_id: uuid.UUID,
        *,
        status: str,
        pending: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        finished: bool = False,
    ) -> None:
        async with self._deps.transaction(str(organization_id)) as session:
            run = await session.get(WorkflowRun, run_id)
            if run is None:
                return
            # A cancellation asked for while the run was reaching its wait wins.
            if run.status == "cancelling" and not finished:
                run.status = "cancelled"
                run.pending = None
                run.finished_at = datetime.now(UTC)
                return
            run.status = status
            run.pending = pending
            run.heartbeat_at = datetime.now(UTC)
            if error is not None:
                run.error = error
            if finished:
                run.finished_at = datetime.now(UTC)


async def recover_interrupted(deps: WorkflowDeps, enqueue: Any) -> list[str]:
    """Find runs whose worker stopped and hand each to a worker again.

    `enqueue(organization_id, run_id)` queues the job. A run is continued from its
    last checkpoint; what that means for a half-finished execution is decided by
    the workflow itself (see `execute_test_cases`). A run that keeps being
    interrupted is failed after a few attempts instead of being tried for ever.
    """
    redis: Any = deps.redis
    recovered: list[str] = []
    for raw in await redis.smembers(ACTIVE_SET):
        member = raw.decode() if isinstance(raw, bytes) else str(raw)
        organization, _, run = member.partition(":")
        try:
            organization_id, run_id = uuid.UUID(organization), uuid.UUID(run)
        except ValueError:
            await redis.srem(ACTIVE_SET, raw)
            continue
        if await redis.exists(lock_key(run_id)):
            continue  # a worker is on it
        attempts = await redis.incr(recoveries_key(run_id))
        await redis.expire(recoveries_key(run_id), 86_400)
        if attempts > MAX_RECOVERIES:
            await redis.srem(ACTIVE_SET, raw)
            async with deps.transaction(organization) as session:
                stored = await session.get(WorkflowRun, run_id)
                if stored is not None and stored.status not in ENDED_WORKFLOW_STATUSES:
                    stored.status = "failed"
                    stored.finished_at = datetime.now(UTC)
                    stored.error = {
                        "code": "recovery_limit",
                        "message": f"The run was interrupted more than {MAX_RECOVERIES} times.",
                    }
            continue
        logger.warning("workflow_recovering", extra={"run_id": run, "attempt": attempts})
        await enqueue(organization_id, run_id)
        recovered.append(run)
    return recovered
