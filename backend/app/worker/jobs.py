"""Background job functions."""

import asyncio
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from app.core.metrics import metrics
from app.services.browser_checks import BrowserCheckRunner
from app.services.evidence import evidence_storage
from app.services.evidence_access import prune_expired
from app.services.executions import ExecutionRunner, recover_stale_executions
from app.services.github import GithubClients, GithubWorker
from app.services.instructions import AnalysisRunner
from app.services.test_cases import GenerationRunner
from app.workflow.deps import WorkflowDeps
from app.workflow.runner import WorkflowRunner, recover_interrupted

logger = logging.getLogger(__name__)


async def ping(_ctx: dict[str, Any]) -> str:
    """No-op job used to verify that the queue and worker are wired together."""
    return "pong"


async def analyse_instruction(
    ctx: dict[str, Any], *, organization_id: str, instruction_id: str, run_token: str
) -> str:
    """Analyse one testing instruction. Enqueued by `InstructionService`."""
    runner = AnalysisRunner(
        session_factory=ctx["session_factory"],
        gateway=ctx["llm_gateway"],
        settings=ctx["settings"],
    )
    return await runner.run(
        organization_id=uuid.UUID(organization_id),
        instruction_id=uuid.UUID(instruction_id),
        run_token=run_token,
    )


async def generate_cases(
    ctx: dict[str, Any],
    *,
    organization_id: str,
    instruction_id: str,
    run_token: str,
    case_ids: list[str],
    feedback: str | None,
) -> str:
    """Write test cases for an approved requirement, or rewrite selected ones."""
    runner = GenerationRunner(
        session_factory=ctx["session_factory"],
        gateway=ctx["llm_gateway"],
        settings=ctx["settings"],
    )
    return await runner.run(
        organization_id=uuid.UUID(organization_id),
        instruction_id=uuid.UUID(instruction_id),
        run_token=run_token,
        case_ids=[uuid.UUID(c) for c in case_ids],
        feedback=feedback,
    )


async def run_browser_check(
    ctx: dict[str, Any], *, organization_id: str, check_id: str, state_changes_approved: bool
) -> str:
    """Open an environment in a browser and optionally sign in. See `BrowserCheckService`."""
    runner = BrowserCheckRunner(
        session_factory=ctx["session_factory"],
        settings=ctx["settings"],
        redis=ctx["app_redis"],
        cipher=ctx["credential_cipher"],
        manager=ctx["browser_manager"],
    )
    return await runner.run(
        organization_id=uuid.UUID(organization_id),
        check_id=uuid.UUID(check_id),
        state_changes_approved=state_changes_approved,
    )


async def run_execution(ctx: dict[str, Any], *, organization_id: str, execution_id: str) -> str:
    """Run the cases of an execution with the test engine. See `ExecutionService`."""
    runner = ExecutionRunner(
        session_factory=ctx["session_factory"],
        settings=ctx["settings"],
        redis=ctx["app_redis"],
        cipher=ctx["credential_cipher"],
        engine=ctx["engine_client"],
        egress=ctx["egress_client"],
    )
    outcome = await runner.run(
        organization_id=uuid.UUID(organization_id), execution_id=uuid.UUID(execution_id)
    )
    await _publish_to_github(ctx, organization_id, execution_id)
    return outcome


def _github_worker(ctx: dict[str, Any]) -> GithubWorker:
    return GithubWorker(
        session_factory=ctx["session_factory"],
        settings=ctx["settings"],
        redis=ctx["app_redis"],
        queue=ctx.get("job_queue") or ctx["redis"],
        clients=GithubClients(
            ctx["settings"], ctx["credential_cipher"], ctx.get("github_transport")
        ),
    )


async def _publish_to_github(ctx: dict[str, Any], organization_id: str, execution_id: str) -> None:
    """Tell GitHub how an execution it asked for ended. Never fails the job."""
    try:
        await _github_worker(ctx).publish(
            organization_id=uuid.UUID(organization_id), execution_id=uuid.UUID(execution_id)
        )
    except Exception:
        logger.exception("github_publish_failed", extra={"execution_id": execution_id})


async def recover_executions(ctx: dict[str, Any]) -> int:
    """Scheduled: close executions whose worker stopped. See `recover_stale_executions`."""
    runner = ExecutionRunner(
        session_factory=ctx["session_factory"],
        settings=ctx["settings"],
        redis=ctx["app_redis"],
        cipher=ctx["credential_cipher"],
        engine=ctx["engine_client"],
        egress=ctx["egress_client"],
    )
    return len(await recover_stale_executions(runner, ctx["app_redis"]))


async def check_services(ctx: dict[str, Any]) -> dict[str, str]:
    """Scheduled: ask the test engine and the browser runner whether they are well.

    Only the worker can reach them. The answers go to Redis for the API's status
    page and to the metrics; they expire, so a worker that stopped checking does
    not leave a stale "ok" behind.
    """
    redis = ctx["app_redis"]
    found: dict[str, str] = {}
    manager = ctx.get("browser_manager")
    checks = {
        "engine": ctx.get("engine_client"),
        "mcp_runner": getattr(manager, "_runner", None) if manager is not None else None,
    }
    for name, client in checks.items():
        started = time.monotonic()
        if client is None:
            state, detail = "not_configured", None
        else:
            try:
                await asyncio.wait_for(client.health(), timeout=10)
                state, detail = "ok", None
            except Exception as exc:
                state, detail = "unavailable", type(exc).__name__
        found[name] = state
        record = {
            "status": state,
            "detail": detail,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "checked_at": datetime.now(UTC).isoformat(),
        }
        await redis.hset("qa:health", name, json.dumps(record))
        await metrics.gauge("qa_service_up", 1 if state == "ok" else 0, {"service": name})
    await redis.expire("qa:health", 300)
    await metrics.record_process()
    return found


async def process_github_delivery(
    ctx: dict[str, Any], *, organization_id: str, delivery_id: str
) -> str:
    """Start the tests a verified webhook delivery asked for. See `GithubWorker.process`."""
    return await _github_worker(ctx).process(
        organization_id=uuid.UUID(organization_id), delivery_id=uuid.UUID(delivery_id)
    )


def _workflow_deps(ctx: dict[str, Any]) -> WorkflowDeps:
    return WorkflowDeps(
        session_factory=ctx["session_factory"],
        settings=ctx["settings"],
        gateway=ctx["llm_gateway"],
        redis=ctx["app_redis"],
        cipher=ctx["credential_cipher"],
        engine=ctx["engine_client"],
        egress=ctx["egress_client"],
        browser_manager=ctx["browser_manager"],
    )


async def recover_workflows(ctx: dict[str, Any]) -> int:
    """Scheduled: continue workflow runs whose worker stopped. See `recover_interrupted`."""

    async def enqueue(organization_id: uuid.UUID, run_id: uuid.UUID) -> None:
        await ctx["redis"].enqueue_job(
            "run_workflow", organization_id=str(organization_id), run_id=str(run_id), resume=None
        )

    return len(await recover_interrupted(_workflow_deps(ctx), enqueue))


async def prune_evidence(ctx: dict[str, Any]) -> int:
    """Scheduled: delete evidence whose retention has ended."""
    counts = await prune_expired(ctx["session_factory"], evidence_storage(ctx["settings"]))
    return counts["removed"]


async def run_workflow(
    ctx: dict[str, Any], *, organization_id: str, run_id: str, resume: dict[str, Any] | None = None
) -> str:
    """Run the agent workflow until it ends or has to wait for a person. See `app.workflow`."""
    runner = WorkflowRunner(_workflow_deps(ctx))
    return await runner.run(
        organization_id=uuid.UUID(organization_id), run_id=uuid.UUID(run_id), resume=resume
    )
