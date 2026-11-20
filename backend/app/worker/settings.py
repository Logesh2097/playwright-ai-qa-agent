"""arq worker configuration.

Run with:   python -m app.worker
Health:     python -m app.worker --check
"""

import logging
from typing import Any, ClassVar

from arq import cron
from arq.connections import RedisSettings

from app.agent.llm.factory import build_gateway
from app.browser.factory import build_egress_client, build_engine_client, build_session_manager
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.metrics import metrics
from app.core.redis import create_redis
from app.core.security.crypto import CredentialCipher, parse_master_keys
from app.db.session import create_engine, create_session_factory
from app.worker.jobs import (
    analyse_instruction,
    check_services,
    generate_cases,
    ping,
    process_github_delivery,
    prune_evidence,
    recover_executions,
    recover_workflows,
    run_browser_check,
    run_execution,
    run_workflow,
)

logger = logging.getLogger(__name__)


async def on_startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = create_engine(settings)
    ctx["settings"] = settings
    ctx["db_engine"] = engine
    ctx["session_factory"] = create_session_factory(engine)
    ctx["llm_gateway"] = build_gateway(settings)
    # arq's own connection (`ctx["redis"]`) belongs to the queue; application data
    # uses a separate client configured like the API's.
    ctx["app_redis"] = create_redis(settings)
    metrics.bind(ctx["app_redis"], process="worker")
    ctx["credential_cipher"] = CredentialCipher(
        parse_master_keys(settings.credential_master_keys.get_secret_value())
    )
    ctx["browser_manager"] = build_session_manager(settings)
    ctx["engine_client"] = build_engine_client(settings)
    ctx["egress_client"] = build_egress_client(settings)
    logger.info("worker_startup", extra={"environment": settings.app_env})


async def on_shutdown(ctx: dict[str, Any]) -> None:
    await ctx["db_engine"].dispose()
    await ctx["app_redis"].aclose()
    logger.info("worker_shutdown")


class WorkerSettings:
    functions: ClassVar[list[Any]] = [
        ping,
        analyse_instruction,
        generate_cases,
        run_browser_check,
        run_execution,
        run_workflow,
        process_github_delivery,
    ]
    # Twice a minute: pick up workflow runs whose worker stopped.
    cron_jobs: ClassVar[list[Any]] = [
        cron(recover_workflows, second={10, 40}, run_at_startup=True),
        # Twice a minute: close executions whose worker stopped.
        cron(recover_executions, second={20, 50}, run_at_startup=True),
        # Every minute: health of the engine and the browser runner, and this process's footprint.
        cron(check_services, second={5}, run_at_startup=True),
        # Hourly: delete evidence whose retention has ended.
        cron(prune_evidence, minute=17, run_at_startup=True),
    ]
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    health_check_interval = 15
    max_jobs = 10
    # Longer than the longest job: a workflow whose execution and retries each reach
    # their time limit, plus the model calls around them.
    job_timeout = get_settings().execution_timeout_s * 3 + 900
