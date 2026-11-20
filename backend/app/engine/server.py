"""The engine service: starts runs, relays their events, serves their evidence.

    POST   /runs                        start a run; the response is its event stream
    GET    /runs/{id}/artifacts/{name}  one evidence file of a finished run
    DELETE /runs/{id}                   stop the run if needed and delete its files
    GET    /healthz                     capacity
    GET    /livez                       liveness, no authentication

Run with:  uvicorn app.engine.server:create_app --factory --host 0.0.0.0 --port 8902

Closing the connection of `POST /runs` stops the run. That is how cancellation
works, and it also means a worker that dies cannot leave a browser running.
"""

import asyncio
import contextlib
import hmac
import json
import logging
import os
import re
import shutil
import signal
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from fastapi.responses import FileResponse, StreamingResponse

from app.core.logging import configure_logging
from app.engine.config import EngineSettings
from app.engine.protocol import ARTIFACT_NAME, RunFinished, RunPlan

logger = logging.getLogger(__name__)

_RUN_ID = re.compile(r"^[0-9a-f-]{36}$")
_ARTIFACT = re.compile(ARTIFACT_NAME)
_LINE_LIMIT = 8 * 1024 * 1024
_EVIDENCE_SUFFIXES = frozenset({".png", ".webm", ".zip"})


@dataclass(slots=True)
class _Run:
    directory: Path
    process: asyncio.subprocess.Process | None = None
    finished_at: float | None = None


def _aborted(error_type: str, message: str) -> bytes:
    event = RunFinished(status="aborted", error_type=error_type, error_message=message)
    return (json.dumps(event.model_dump(mode="json")) + "\n").encode()


def _kill(process: asyncio.subprocess.Process | None) -> None:
    """The whole process group: the run and every browser process it started."""
    if process is None or process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)


def run_budget_s(plan: RunPlan, ceiling: int) -> float:
    """Time a plan can legitimately need: every case, every attempt, plus sign-in and cleanup."""
    config = plan.config
    per_case = (config.timeouts.test_ms / 1000 + 120) * config.retry.max_attempts
    return float(min(ceiling, 60 + per_case * len(plan.cases)))


def create_app(settings: EngineSettings | None = None) -> FastAPI:
    settings = settings or EngineSettings()
    configure_logging(settings.log_level)
    work_dir = Path(settings.engine_work_dir)
    runs: dict[str, _Run] = {}

    def remove(run_id: str) -> bool:
        run = runs.pop(run_id, None)
        if run is None:
            return False
        _kill(run.process)
        shutil.rmtree(run.directory, ignore_errors=True)
        return True

    async def reap() -> None:
        while True:
            await asyncio.sleep(15)
            now = time.monotonic()
            for run_id, run in list(runs.items()):
                if (
                    run.finished_at is not None
                    and now - run.finished_at > settings.engine_artifact_ttl_s
                ):
                    remove(run_id)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        shutil.rmtree(work_dir, ignore_errors=True)
        work_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - once, at start-up
        reaper = asyncio.create_task(reap())
        try:
            yield
        finally:
            reaper.cancel()
            for run_id in list(runs):
                remove(run_id)

    app = FastAPI(
        title="engine", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.runs = runs

    def authorize(authorization: Annotated[str, Header()] = "") -> None:
        expected = f"Bearer {settings.engine_token.get_secret_value()}"
        if not hmac.compare_digest(authorization.encode(), expected.encode()):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED)

    def active() -> int:
        return sum(1 for run in runs.values() if run.finished_at is None)

    @app.get("/livez")
    async def livez() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/healthz", dependencies=[Depends(authorize)])
    async def healthz() -> dict[str, Any]:
        from playwright._repo_version import version as playwright_version

        return {
            "status": "ok",
            "runs": active(),
            "max_runs": settings.engine_max_runs,
            "playwright_version": playwright_version,
            "browsers": ["chromium", "firefox", "webkit"],
        }

    @app.post("/runs", dependencies=[Depends(authorize)])
    async def start(plan: RunPlan) -> StreamingResponse:
        if plan.proxy is None and settings.engine_require_proxy:
            raise HTTPException(422, "a proxy is required")
        if plan.run_id in runs:
            raise HTTPException(status.HTTP_409_CONFLICT, "run already exists")
        if active() >= settings.engine_max_runs:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "all run slots are in use")
        directory = work_dir / plan.run_id
        directory.mkdir(mode=0o700)
        run = _Run(directory)
        runs[plan.run_id] = run
        budget = run_budget_s(plan, settings.engine_max_run_seconds)

        async def events() -> AsyncIterator[bytes]:
            finished = False
            try:
                log = (directory / "engine.log").open("wb")
                try:
                    process = await asyncio.create_subprocess_exec(
                        sys.executable, "-m", "app.engine.child", str(directory),
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=log,
                        limit=_LINE_LIMIT,
                        start_new_session=True,
                        cwd=str(directory),
                        # Nothing of the service's own environment, least of all its token.
                        env={
                            "PATH": os.environ.get("PATH", ""),
                            "HOME": str(directory),
                            "PYTHONPATH": os.environ.get("PYTHONPATH", os.getcwd()),
                            "PLAYWRIGHT_BROWSERS_PATH": os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""),
                        },
                    )  # fmt: skip
                finally:
                    log.close()
                run.process = process
                assert process.stdin and process.stdout  # noqa: S101
                process.stdin.write(plan.model_dump_json().encode())
                await process.stdin.drain()
                process.stdin.close()
                deadline = time.monotonic() + budget
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    line = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
                    if not line:
                        break
                    finished = finished or b'"event":"run_finished"' in line
                    yield line
                await process.wait()
                if not finished:
                    finished = True
                    yield _aborted(
                        "internal", f"The run ended without a result (exit {process.returncode})."
                    )
            except TimeoutError:
                finished = True
                yield _aborted("run_timeout", f"The run was stopped after {budget:g} seconds.")
            finally:
                # Also reached when the caller disconnects: the run must not outlive it.
                _kill(run.process)
                run.finished_at = time.monotonic()
                logger.info("engine_run_ended", extra={"run_id": plan.run_id, "complete": finished})

        logger.info("engine_run_started", extra={"run_id": plan.run_id, "cases": len(plan.cases)})
        return StreamingResponse(events(), media_type="application/x-ndjson")

    @app.get("/runs/{run_id}/artifacts/{name}", dependencies=[Depends(authorize)])
    async def artifact(run_id: str, name: str) -> FileResponse:
        run = runs.get(run_id) if _RUN_ID.fullmatch(run_id) else None
        if run is None or not _ARTIFACT.fullmatch(name):
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        path = run.directory / name
        # Evidence files directly in the run directory only: not the run's log, not
        # the uploaded test files.
        if (
            path.suffix not in _EVIDENCE_SUFFIXES
            or path.parent != run.directory
            or not path.is_file()
            or path.is_symlink()
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        return FileResponse(path)

    @app.delete(
        "/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(authorize)]
    )
    async def delete(run_id: str) -> Response:
        if not remove(run_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app
