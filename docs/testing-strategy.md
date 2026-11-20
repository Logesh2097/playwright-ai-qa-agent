# Testing Strategy

This document covers two things: how the platform itself is tested, and how the quality of the AI agent is evaluated. (How the platform tests *other* applications is in [architecture.md](architecture.md) §6–7 and [playwright-mcp-integration.md](playwright-mcp-integration.md).)

## 1. Tooling

| Area | Tools |
|------|-------|
| Backend tests | Pytest, pytest-asyncio, httpx `AsyncClient` against the ASGI app |
| Backend static checks | Ruff (lint + format), mypy in strict mode |
| Frontend tests | Vitest, Vue Test Utils |
| Frontend static checks | ESLint, `vue-tsc` |
| End-to-end | Playwright (Python) driving the SPA against the Compose stack |
| Target fixture | `fixtures/demo-app` — a small web app with seeded, documented defects |

No additional test frameworks are introduced.

## 2. Test levels

### 2.1 Unit tests

Fast, no network, no database.

| Subject | Examples |
|---------|----------|
| Step DSL models | Valid and invalid steps; locator policy; placeholder parsing. |
| Scenario validator | Each rule in [agent-workflow.md](agent-workflow.md) §6 has a passing and failing case. |
| Verdict logic | Step outcomes → result status; results → execution verdict; `llm_inferred` oracle → `inconclusive`; a pass on retry → `flaky`. |
| Retry decision table | Every failure class × attempt count. |
| Safe expression evaluator | Arithmetic, allowed functions, rejection of names, attribute access, calls outside the whitelist. |
| URL and origin validation | Private ranges, IPv6 forms, IPv4-mapped addresses, userinfo, punycode, ports. |
| Encryption | Round trip, wrong associated data fails, key-version rotation. |
| Redaction | Headers, tokens, session secrets, nested JSON. |
| Auth | Password hashing, token rotation and reuse detection, API key hashing, role comparisons. |
| Report builders | JUnit XML validity; totals; HTML escaping of page-derived text. |
| LangGraph routing | Conditional edge functions as pure functions of state. |

### 2.2 Integration tests

Real Postgres and Redis (Compose services in CI), LLM and runner faked.

| Subject | Examples |
|---------|----------|
| Repositories and RLS | A query in organization A never returns organization B's rows — parametrised over every tenant table. |
| Migrations | Upgrade from empty to head; downgrade one step; model/migration drift check. |
| API | Every endpoint: success, validation failure, unauthenticated, wrong role, cross-tenant (expects 404). |
| Idempotency and concurrency | Replayed `Idempotency-Key`; `If-Match` conflicts; organization concurrency limit. |
| Jobs | Enqueue, run, retry after simulated crash, lock contention. |
| Graph with fakes | Whole workflow with a scripted fake LLM and fake runner: happy path, clarification, approval reject, validation regeneration, retry rounds, budget exhaustion, cancellation at each node, resume from checkpoint. |
| SSE | Event ordering, reconnect with `Last-Event-ID`, ticket expiry. |
| GitHub | Webhook signature verification, deduplication, mapping lookup, check-run payloads (HTTP mocked). |

### 2.3 Engine and runner tests

Real browsers, against `fixtures/demo-app` served locally.

- Every DSL action and assertion, passing and failing.
- Locator strictness (ambiguous locator → error) and fallback behaviour.
- Evidence produced for a failed step: screenshot, trace, console and network logs; masks applied.
- Read-only routing blocks `POST` on a read-only environment.
- Off-allowlist request is aborted and recorded.
- Viewport matrix and layout assertions on a page with a known overflow defect.
- API steps sharing session cookies with UI steps.
- Session isolation: two sessions do not share cookies, storage, or files.
- Cancellation mid-case; session reaper cleans up.

### 2.4 MCP integration tests

Real Playwright MCP server at the pinned version.

- Tool discovery returns the tools the allowlist expects (a contract test that fails loudly on upgrade).
- Allowlist filtering; blocked tools are not bound.
- Off-scope navigation rejected by the client and, independently, by the proxy.
- Storage-state handoff: MCP session starts authenticated.
- Result truncation and redaction.
- Server killed mid-loop → reconnect path → degraded path.

### 2.5 Frontend tests

- Unit: stores (reducers for REST + SSE merge), composables, route guards, formatting utilities.
- Component: UI primitives; domain components such as the step editor, status badges, evidence viewer, approval dialog.
- Types generated from OpenAPI are checked in CI so a backend contract change breaks the frontend build rather than production.

### 2.6 End-to-end tests

A small set against the full Compose stack with a recorded/fake LLM:

1. Register → create project, application, environment, credential.
2. Submit an instruction → answer a clarification → review and approve cases.
3. Run a suite → watch live progress → open a failed result → view evidence.
4. Download the report; cancel a running execution; reject an approval.

### 2.7 Security tests

| Area | Test |
|------|------|
| Tenant isolation | Automated cross-tenant sweep over all endpoints with IDs from another organization. |
| SSRF | Environment URLs and redirects pointing at loopback, private ranges, metadata address, DNS names that resolve to private addresses, rebinding simulation at the proxy. |
| Prompt injection | Demo-app pages containing injected instructions (navigate elsewhere, reveal credentials, declare success). Assert: no off-scope request leaves the proxy, no secret appears in LLM inputs, verdicts are unchanged. |
| Secrets | Run a login flow, then scan all logs, agent logs, evidence metadata, traces and LLM request captures for the credential value. |
| Authorization | Role matrix test generated from the endpoint/role table. |
| Sandbox | Runner cannot reach Postgres/Redis; cannot reach the internet except via proxy; root filesystem is read-only. |
| Webhooks | Invalid signature, replayed delivery, oversized body. |
| Output encoding | Page text containing HTML/script appears escaped in the UI and HTML report. |

### 2.8 Reliability and performance tests

- Kill the worker mid-execution: the run resumes or ends in a terminal state; nothing stays `running`.
- Kill the runner mid-execution: affected results become `error` (`infrastructure`) and are retried in a fresh session.
- Redis restart: queued executions are re-enqueued by the reconciler.
- OpenAI outage (fake returning 5xx): `run_existing` completes without analysis.
- Load: N concurrent executions per runner; API latency under concurrent CRUD; SSE fan-out with many subscribers.

### Browser integration tests (added in Phase 6)

| Suite | Command | Uses |
|-------|---------|------|
| MCP client, tool permissions, redaction, sessions, cleanup, login, agent tools | `make be-test` | A mock MCP server: a real server built with the MCP SDK over in-memory streams, with a pretend browser behind it (`tests/mcp_fakes.py`). |
| Egress proxy | `make be-test` | Real loopback sockets, fake DNS and upstream. |
| Browser checks through the API | `make be-test-integration` | Real PostgreSQL and Redis; mock MCP server. |
| Runner supervisor | `make runner-test` | A stand-in server process; standard library `unittest`. |
| Real server and browser | `make be-test-browser` | Pinned Playwright MCP, Chromium, the egress proxy and `fixtures/demo-app`. Includes the contract test that ties the permission table to the pinned version. |

### Test engine tests (added in Phase 7)

| Suite | Command | Uses |
|-------|---------|------|
| Step schema, pre-run checks, business-rule expressions, text matching, verdict rules, wire format, engine client, evidence store | `make be-test` | Nothing: pure code. |
| The engine itself: every action, locator strategy and assertion with passing and failing cases; timeouts; cleanup; retries; sign-in; isolation; secrets; evidence; three browsers; the engine service | `make engine-test` | The engine image with real browsers and `fixtures/demo-app`, reached directly. |
| Executions through the API | `make be-test-integration` | Real PostgreSQL and Redis; a stand-in engine that answers with scripted events (`tests/engine_fakes.py`). |
| End to end | `make be-test-browser` | Nothing faked: API, worker code, engine service, egress proxy, browsers, demo application. |

None of these configure or call a model. The end-to-end suite asserts that the model stand-in received no request.

### Agent workflow tests (added in Phase 8)

| Suite | Command | Uses |
|-------|---------|------|
| The graph's shape, every conditional edge, retry rules, tool selection, script conversion, discovery parsing, the report, strict-mode schemas | `make be-test` | Nothing: pure code. |
| The workflow through the API: success, incomplete requirement, rejection, browser failures, assertion failures, retries, recovery after an interruption, cancellation, permissions, secrets in state | `make be-test-integration` | Real PostgreSQL, Redis and LangGraph checkpoints; a scripted model (`tests/workflow_fakes.py`), the stand-in engine and the mock MCP server. |
| The workflow end to end | `make be-test-browser` | Real MCP discovery through runner and proxy, real engine, demo application; only the model is scripted. |

### Result validation and self-healing tests (added in Phase 9)

| Suite | Command | Uses |
|-------|---------|------|
| Classification table, verdict precedence, failure records, suggestion labelling, flaky rules | `make be-test` | Nothing: pure code. |
| Locator recovery: renamed elements, ambiguous, opposite and destructive names, wrong role, limits | `make engine-test` | Real browser and the demo application's `/healing` page. |
| Categories as stored, failure reports, a model that claims a pass, proposals, stability | `make be-test-integration` | Real PostgreSQL and Redis; stand-in engine and model. |
| Error → recovered → accepted → approved → passes as written | `make be-test-browser` | Nothing faked; no model. |

False positives are tested as such: each test names a replacement the engine must *not* use and checks that nothing was clicked.

### Reports and evidence tests (added in Phase 10)

| Suite | Command | Uses |
|-------|---------|------|
| File validation, storage isolation, download tokens, report assembly, exports, analytics definitions | `make be-test` | Nothing: pure code. |
| Report accuracy, exports, evidence permissions, download addresses, limits, retention, history, dashboard | `make be-test-integration` | Real PostgreSQL and Redis; stand-in engine. |
| A real run with real screenshot, video and trace; all export formats | `make be-test-browser` | Nothing faked. |
| Report page, dashboard, chart, formatting | `make fe-test` | jsdom; the API module is replaced. |

Report accuracy is tested by comparison: the report's counts must equal the execution's own totals and the statuses of the stored results.

### GitHub and CI tests (added in Phase 11)

| Suite | Command | Uses |
|-------|---------|------|
| Signatures, events, selection, API client failures, CI outcome, JUnit, comment escaping, the CI script | `make be-test` | A mock HTTP transport for GitHub; a local stub server for the script. |
| Connecting, linking, webhooks, pull request events, duplicates, triggering, statuses and comments, GitHub failures, API keys, CI endpoints | `make be-test-integration` | Real PostgreSQL and Redis; stand-in GitHub and engine. |

GitHub itself is never called by a test. The stand-in answers only the REST paths the platform is meant to use, so a call to anything else fails the test.

A worker stopping is simulated by raising an exception that nothing catches from inside a node, so that, as with a killed process, no clean-up code runs. The same was done for real by killing the worker container during an execution.

The mock server covers failure paths that are hard to produce on demand (lost connection, hung handshake, broken discovery). The real-server suite covers what a mock cannot: the server's actual tools, result layout and proxy behaviour.

## 3. LLM testing approach

LLM calls are non-deterministic and cost money, so tests are tiered:

| Tier | LLM | Runs | Purpose |
|------|-----|------|---------|
| 1 | Scripted fake returning fixed structured outputs | Every commit | Workflow logic. |
| 2 | Recorded responses replayed from fixtures | Every commit | Prompt/schema compatibility; parsing; repair path. |
| 3 | Live model, small smoke set | Nightly and before release | Detect provider or model drift. |
| 4 | Live model, full evaluation suite | Before release; on prompt or model change | Quality metrics (section 4). |

Prompts are versioned; a prompt change requires a tier-4 run and the results are attached to the pull request.

## 4. Agent evaluation

### 4.1 Fixture

`fixtures/demo-app` implements login, registration, product creation, a loyalty-points calculator, a small REST API and responsive pages. It ships with a manifest of **seeded defects** (for example: a dead button, a missing required-field validation, an off-by-one in points rounding, a 500 on a specific input, a horizontal overflow at 375 px) and a list of **known-good behaviours**.

### 4.2 Golden set

About 30–50 instructions modelled on the brief's examples, each with:

- reference scenarios that a competent tester would write,
- the seeded defects the instruction should expose,
- behaviours that must not be reported as failures.

### 4.3 Metrics and gates

| Metric | Definition | Release gate |
|--------|------------|--------------|
| False-pass rate | Results marked `passed` where a seeded defect was exercised by that case. | **0** |
| LLM-originated passes | Results whose pass depends on anything but deterministic assertions (checked structurally). | **0** |
| Schema-valid generation rate | Generated cases passing validation without regeneration. | ≥ 90% |
| Scenario coverage | Reference scenarios matched by a generated case. | ≥ 80% |
| Defect detection rate | Seeded defects in scope that end as `failed` or as a finding. | ≥ 80% |
| False-fail rate | `failed` results on known-good behaviour. | ≤ 5% |
| Locator validity | Generated locators that resolve uniquely on first run. | ≥ 90% |
| Heal precision | Accepted-by-rerun heal proposals that target the intended element (manual label). | ≥ 95% |
| Failure-class accuracy | Agreement with labelled classes. | ≥ 85% |
| Clarification quality | Ambiguous instructions that trigger a question; clear ones that do not. | tracked |
| Cost and duration | Per instruction and per execution. | within budget |
| Scope violations | Off-allowlist requests reaching the proxy log as allowed. | **0** |

Thresholds other than the zero-gates are initial targets to be calibrated after the first evaluation run.

Evaluation results are stored per run (prompt version, model, metrics) so regressions are visible over time.

## 5. CI gates

> **As built (Phase 14).** The list below is the original plan. What the CI
> workflow actually does, and the current numbers, are in
> [testing-guide.md](testing-guide.md) §6. Not built: the OpenAPI schema diff,
> image vulnerability scanning, nightly runs, and enforced coverage targets.
> Agent evaluation as built is described in
> [evaluation-results.md](evaluation-results.md); it differs from §4 in that no
> model-based tier has been run.

On every pull request:

1. Backend: `ruff check`, `ruff format --check`, `mypy`, unit tests, integration tests (Postgres + Redis services).
2. Frontend: `eslint`, `vue-tsc`, `vitest`, build.
3. Engine/runner tests with browsers.
4. OpenAPI schema diff; generated frontend types up to date.
5. Migration checks.
6. Dependency and image vulnerability scan; secret scan.

On main and nightly: end-to-end suite, MCP integration tests, tier-3 LLM smoke.
Before release: tier-4 evaluation, security test suite, reliability tests.

Coverage targets: 85% line coverage on backend services, validators and verdict logic at 100% branch coverage; 70% on frontend stores and composables.

## 6. Test data

- Factories create tenant-scoped data for tests; no shared mutable fixtures.
- The demo app resets to a known state before each engine/e2e test run.
- No real credentials or personal data in fixtures; recorded LLM fixtures are scanned for secrets before commit.
