"""Operational metrics, in the Prometheus text format.

Counters live in one Redis hash, so the API processes and the worker add to the
same numbers and any API process can serve them. Recording never raises and
never blocks a request for long: if Redis is unavailable the sample is dropped.

Nothing identifying is recorded: no organization, user, project, address or
test content. Labels are small fixed sets (a route template, a status, a model
name, a tool name).

    qa_http_requests_total{method,route,status}
    qa_http_request_duration_seconds_{sum,count}{method,route}
    qa_http_errors_total{route,status}                 5xx answers
    qa_rejected_requests_total{reason}                 too large, rate limited
    qa_executions_total{status,verdict}
    qa_execution_duration_seconds_{sum,count}          browser execution time
    qa_execution_queue_delay_seconds_{sum,count}       queued -> started
    qa_test_results_total{status}                      includes failed and error
    qa_llm_calls_total{purpose,model,status}
    qa_llm_latency_seconds_{sum,count}{purpose,model}
    qa_llm_tokens_total{model,direction}
    qa_llm_cost_usd_total{model}
    qa_mcp_tool_calls_total{tool,status}
    qa_mcp_tool_duration_seconds_{sum,count}{tool}
    qa_agent_node_failures_total{node}
    qa_jobs_recovered_total{kind}
    qa_process_resident_memory_bytes{process}          gauge
    qa_process_cpu_seconds_total{process}              gauge
    qa_service_up{service}                             gauge, from the worker's health checks
"""

import contextlib
import os
import resource
import time
from collections.abc import Mapping
from typing import Any

KEY = "qa:metrics"
GAUGES = "qa:metrics:gauges"
_HELP = {
    "qa_http_requests_total": "HTTP requests answered.",
    "qa_http_errors_total": "HTTP requests answered with a server error.",
    "qa_executions_total": "Test executions that ended.",
    "qa_test_results_total": "Test case results recorded.",
    "qa_llm_calls_total": "Calls to the model provider.",
    "qa_mcp_tool_calls_total": "Browser tool calls through the MCP server.",
    "qa_agent_node_failures_total": "Workflow nodes that ended in an error.",
}


def _name(metric: str, labels: Mapping[str, Any] | None) -> str:
    if not labels:
        return metric
    inner = ",".join(
        f'{key}="{_escape(value)}"' for key, value in sorted(labels.items()) if value is not None
    )
    return f"{metric}{{{inner}}}"


def _escape(value: Any) -> str:
    return str(value)[:120].replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


class Metrics:
    """Process-wide. Bound to Redis once at start-up; a no-op until then."""

    def __init__(self) -> None:
        self._redis: Any = None
        self._process = "api"

    def bind(self, redis: Any, *, process: str) -> None:
        self._redis = redis
        self._process = process

    async def add(self, samples: Mapping[str, float]) -> None:
        if self._redis is None or not samples:
            return
        with contextlib.suppress(Exception):
            async with self._redis.pipeline(transaction=False) as pipe:
                for name, value in samples.items():
                    pipe.hincrbyfloat(KEY, name, value)
                await pipe.execute()

    async def inc(
        self, metric: str, labels: Mapping[str, Any] | None = None, value: float = 1
    ) -> None:
        await self.add({_name(metric, labels): value})

    async def observe(
        self, metric: str, seconds: float, labels: Mapping[str, Any] | None = None
    ) -> None:
        """Adds to `<metric>_sum` and `<metric>_count`."""
        await self.add(
            {_name(f"{metric}_sum", labels): seconds, _name(f"{metric}_count", labels): 1}
        )

    async def gauge(
        self, metric: str, value: float, labels: Mapping[str, Any] | None = None
    ) -> None:
        if self._redis is None:
            return
        with contextlib.suppress(Exception):
            await self._redis.hset(GAUGES, _name(metric, labels), value)
            await self._redis.expire(GAUGES, 600)

    async def record_process(self) -> None:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        labels = {"process": self._process}
        # ru_maxrss is kilobytes on Linux.
        await self.gauge("qa_process_resident_memory_bytes", usage.ru_maxrss * 1024, labels)
        await self.gauge("qa_process_cpu_seconds_total", usage.ru_utime + usage.ru_stime, labels)

    async def render(self) -> str:
        """Everything recorded, as Prometheus text. Empty if Redis cannot be read."""
        if self._redis is None:
            return ""
        await self.record_process()
        try:
            counters = await self._redis.hgetall(KEY)
            gauges = await self._redis.hgetall(GAUGES)
        except Exception:
            return ""
        lines: list[str] = []
        seen: set[str] = set()
        for source, kind in ((counters, "counter"), (gauges, "gauge")):
            for raw_name in sorted(source):
                name = raw_name.decode() if isinstance(raw_name, bytes) else str(raw_name)
                raw_value = source[raw_name]
                value = raw_value.decode() if isinstance(raw_value, bytes) else str(raw_value)
                family = name.split("{", 1)[0]
                if family not in seen:
                    seen.add(family)
                    if family in _HELP:
                        lines.append(f"# HELP {family} {_HELP[family]}")
                    lines.append(f"# TYPE {family} {kind}")
                lines.append(f"{name} {float(value):g}")
        lines.append(f"# scraped_at {int(time.time())} pid {os.getpid()}")
        return "\n".join(lines) + "\n"


metrics = Metrics()
