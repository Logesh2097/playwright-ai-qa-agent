# Operations: Monitoring, Recovery and Limits

What to watch, what recovers by itself, and what an operator has to do
(Phase 13).

## 1. Health

| Endpoint | Meaning |
|----------|---------|
| `GET /healthz` | The API process is up. Touches nothing else |
| `GET /readyz` | PostgreSQL and Redis are reachable; 503 with details when not |
| `GET /api/v1/system/services` (signed in) | The test engine and the browser (MCP) runner, as the worker last saw them: status, latency, when checked. Entries expire after five minutes, so a worker that stopped checking does not leave a stale "ok" |

Every container has a health check in Compose. The worker checks the engine
and the runner every minute (`check_services`), because only the worker can
reach them.

## 2. Metrics

`GET /metrics` returns Prometheus text. It exists only when `METRICS_TOKEN` is
set (24 characters or more) and needs `Authorization: Bearer <token>`. Counters
are kept in Redis, so the API and the worker add to the same numbers.

| Metric | Tells you |
|--------|-----------|
| `qa_http_requests_total{method,route,status}` | Traffic and answers per route template |
| `qa_http_request_duration_seconds_{sum,count}` | API latency |
| `qa_http_errors_total{route,status}` | **API errors** (5xx) |
| `qa_rejected_requests_total{reason}` | Oversized bodies, rate-limited requests |
| `qa_executions_total{status,verdict}` | Executions ended, by outcome |
| `qa_execution_duration_seconds_{sum,count}` | **Browser execution duration** |
| `qa_execution_queue_delay_seconds_{sum,count}` | **Queue delay**: queued until a worker started |
| `qa_test_results_total{status}` | **Test failures** and every other result |
| `qa_llm_calls_total{purpose,model,status}` | Model calls and their failures |
| `qa_llm_latency_seconds_{sum,count}` | **LLM latency** |
| `qa_llm_tokens_total{model,direction}`, `qa_llm_cost_usd_total{model}` | **Token usage** and cost |
| `qa_mcp_tool_calls_total{tool,status}`, `qa_mcp_tool_duration_seconds_{sum,count}` | **MCP latency** and failures |
| `qa_agent_node_failures_total{node}` | **Agent failures**, by workflow node |
| `qa_jobs_recovered_total{kind}` | Executions closed after a worker stopped |
| `qa_service_up{service}` | Engine and runner health |
| `qa_process_resident_memory_bytes{process}`, `qa_process_cpu_seconds_total{process}` | **Resource consumption** of the API and worker processes |

Labels are small fixed sets. No organization, user, project, address or test
content is ever a label value.

Container-level CPU and memory (browsers included) come from the container
runtime (`docker stats`, cAdvisor); the engine and runner have memory, CPU and
process limits set in Compose.

Suggested alerts: any `qa_http_errors_total` increase; `qa_service_up == 0` for
more than two minutes; rising `qa_execution_queue_delay_seconds` average;
`qa_jobs_recovered_total` increasing; `qa_llm_calls_total{status!="ok"}` rate.

## 3. Logs

One JSON object per line on standard output, with a request ID on every line
of a request. Secrets are not logged: credentials are redacted where they are
handed out, tokens never appear in URLs, and emails with single-use links are
not logged in production. Useful messages: `request`, `unhandled_exception`,
`execution_failed`, `execution_recovered`, `workflow_node_failed`,
`mcp_tool_call`, `github_status_failed`, `evidence_pruned`.

## 4. What recovers by itself

| Situation | What happens |
|-----------|--------------|
| Worker stops during a workflow run | The next worker continues the run from its checkpoint, at most three times |
| Worker stops during an execution | Within about two minutes the execution is closed: the test in progress is an error (`worker_interrupted`), tests not started are skipped. Nothing is run a second time, because what a half-finished test did is unknown |
| A job is delivered twice | The second delivery finds the execution already claimed and does nothing |
| Engine or runner unavailable | Executions fail with `engine_unavailable`, never pass; a workflow retries within its limit |
| Execution exceeds its time | Stopped and marked `timed_out` |
| Redis unavailable | Metrics and live progress are dropped silently; rate limits and queued jobs need Redis, so the API answers 503 on `/readyz` |
| GitHub unavailable | Tests still run (the full set); publishing failures are recorded and change no result |
| Evidence past retention | Deleted hourly |

## 5. Limits

| Limit | Setting | Default |
|-------|---------|---------|
| Request body | `MAX_REQUEST_BODY_BYTES` | 4 MB |
| Webhook payload | `GITHUB_WEBHOOK_MAX_BYTES` | 1 MB |
| Executions queued or running per organization | `EXECUTION_MAX_ACTIVE` | 3 |
| Executions started per organization per hour | `EXECUTION_LIMIT` | 60 |
| Cases per execution | `EXECUTION_MAX_CASES` | 200 |
| Execution time | `EXECUTION_TIMEOUT_S` | 30 minutes |
| Time without a sign of life before an execution is closed | `EXECUTION_STALE_AFTER_S` | 90 s |
| Requests per API key per minute | `CI_RATE_LIMIT` | 120 |
| Evidence per file / per execution | `EVIDENCE_MAX_*_BYTES` | 10 MB / 100 MB / 1 MB; 1 GB |
| Model spend per month | `LLM_MONTHLY_BUDGET_USD` | unset |

## 6. Operator tasks

- **Rotate the credential master key**: add a new version to
  `CREDENTIAL_MASTER_KEYS`; new secrets use it, old ones stay readable until
  re-saved.
- **Rotate the JWT key**: changing `JWT_SECRET_KEY` signs everyone out and
  invalidates outstanding evidence download addresses.
- **Rotate a GitHub webhook secret or an API key**: from the application; the
  old one stops working at once.
- **Back up** PostgreSQL and the evidence volume together (see
  [deployment.md](deployment.md)).
- **Stopping**: `docker compose stop` gives the worker 60 seconds; anything
  still running is closed by the recovery jobs when a worker starts again.
