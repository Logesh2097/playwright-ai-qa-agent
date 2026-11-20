"""Worker entry point: ``python -m app.worker`` (``--check`` for the health probe).

Used instead of the ``arq`` CLI so that every log line, including arq's own,
goes through the JSON formatter.
"""

import logging
import sys

from arq.worker import check_health, run_worker

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.worker.settings import WorkerSettings


def main() -> int:
    configure_logging(get_settings().log_level)
    arq_logger = logging.getLogger("arq")
    arq_logger.handlers = []
    arq_logger.propagate = True

    if "--check" in sys.argv[1:]:
        return check_health(WorkerSettings)  # type: ignore[arg-type]
    run_worker(WorkerSettings)  # type: ignore[arg-type]
    return 0


if __name__ == "__main__":
    sys.exit(main())
