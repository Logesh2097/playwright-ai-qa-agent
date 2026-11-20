"""One run, in a process of its own.

    python -m app.engine.child <run directory>

The plan arrives on standard input, because it carries test credentials and so
must not appear in a command line or a file. Events leave on standard output,
one JSON object per line. Diagnostics go to standard error.

A separate process per run means a run can be stopped for certain (its whole
process group is killed) and cannot affect another run or the service.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.engine.protocol import RunFinished, RunPlan
from app.engine.runner import run_plan


async def _emit(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    run_dir = Path(sys.argv[1])
    try:
        plan = RunPlan.model_validate_json(sys.stdin.buffer.read())
    except ValidationError as exc:
        # Field locations only: the input holds credentials.
        where = sorted({".".join(str(p) for p in error["loc"]) for error in exc.errors()})[:10]
        asyncio.run(
            _emit(
                RunFinished(
                    status="aborted",
                    error_type="invalid_plan",
                    error_message="The plan is not valid: " + ", ".join(where),
                ).model_dump(mode="json")
            )
        )
        return 2
    asyncio.run(run_plan(plan, run_dir, _emit))
    return 0


if __name__ == "__main__":
    sys.exit(main())
