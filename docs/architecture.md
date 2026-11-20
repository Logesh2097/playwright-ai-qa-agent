# Architecture

Status: design document. Implementation status is tracked in [progress.md](progress.md); Phase 1 (foundation) is complete.

Related documents: [implementation-plan.md](implementation-plan.md), [database-design.md](database-design.md), [api-design.md](api-design.md), [agent-workflow.md](agent-workflow.md), [playwright-mcp-integration.md](playwright-mcp-integration.md), [security-design.md](security-design.md), [testing-strategy.md](testing-strategy.md), [deployment.md](deployment.md), [risks-and-limitations.md](risks-and-limitations.md).

## 1. Product summary

An AI-assisted QA platform. A developer or QA engineer writes an instruction in plain language ("Test the customer login functionality"), and the platform:

1. analyses the requirement,
2. produces a test plan and structured test cases,
3. gets human approval where required,
4. executes the cases in a real browser,
5. decides pass/fail from deterministic assertions,
6. investigates failures, captures evidence, and
7. publishes a report (UI, JSON, JUnit, GitHub check).

## 2. Requirements

### 2.1 Functional requirements

| ID | Requirement |
|----|-------------|
| FR-01 | Users register, log in, and belong to one or more organizations with a role. |
| FR-02 | Organizations own projects; projects own applications; applications own environments (base URL, allowed origins, credentials). |
| FR-03 | Users submit natural-language test instructions against an application + environment. |
| FR-04 | The agent analyses the instruction, asks clarifying questions when the requirement is ambiguous, and produces a structured requirement analysis. |
| FR-05 | The agent generates a test plan and test cases (UI, API, responsive, audit) in a structured step format. |
| FR-06 | Users review, edit, approve, or reject generated cases; cases are versioned and grouped into suites. |
| FR-07 | The platform executes cases with Playwright: UI interaction, API calls, multiple viewports, data-driven rows. |
| FR-08 | Verdicts come from deterministic assertions. The LLM never upgrades a result to "passed". |
| FR-09 | The agent can explore the application through Playwright MCP to ground test generation and to investigate failures. |
| FR-10 | Failures are classified; eligible failures are retried under fixed rules; broken locators can be healed with human sign-off. |
| FR-11 | Screenshots, video, traces, console and network logs are captured, redacted, stored, and linked to steps. |
| FR-12 | Executions can be watched live, cancelled, and browsed historically. |
| FR-13 | Reports are generated per execution (HTML, JSON, JUnit XML) and summarised on a dashboard. |
| FR-14 | GitHub integration: trigger runs from PRs/deployments, publish check runs and PR comments, open issues for failures. |
| FR-15 | CI integration: API-key-authenticated trigger and polling endpoints, JUnit output, exit-code semantics. |
| FR-16 | Notifications on execution completion, failure, and pending approvals. |
| FR-17 | Usage tracking for LLM tokens/cost and agent activity; audit log of security-relevant actions. |
| FR-18 | MCP connection management: configure, health-check, and restrict the tools available to the agent. |

### 2.2 Non-functional requirements

| ID | Area | Target |
|----|------|--------|
| NFR-01 | Tenant isolation | No cross-organization data access; every tenant row carries `organization_id`; enforced in the data layer and by Postgres RLS. |
| NFR-02 | Verdict integrity | Zero LLM-originated passes. Measured in the evaluation suite as "false-pass rate = 0". |
| NFR-03 | Scope safety | The browser can only reach origins on the environment allowlist, enforced at the network layer, not by the prompt. |
| NFR-04 | Reproducibility | A stored test case version re-runs without any LLM call. |
| NFR-05 | Responsiveness | API p95 < 300 ms for CRUD; execution start < 10 s after dequeue; live events delivered < 2 s after they occur. |
| NFR-06 | Bounded cost | Per-execution caps on wall time, LLM calls, tool calls, and USD cost; per-organization monthly budget. |
| NFR-07 | Reliability | A worker or runner crash leaves the execution resumable or cleanly marked `error`; no execution stays `running` forever. |
| NFR-08 | Scalability | Workers and runners scale horizontally; API is stateless. |
| NFR-09 | Observability | Structured JSON logs with request/execution correlation IDs; every LLM and tool call recorded. |
| NFR-10 | Maintainability | Ruff + mypy (strict) clean; ESLint + `vue-tsc` clean; CI gates on all of them. |
| NFR-11 | Data protection | Credentials encrypted at rest; secrets never sent to the LLM; evidence redacted and retention-limited. |

## 3. System architecture

```
                        ┌──────────────────────────────┐
                        │  Vue 3 SPA (Vite, Pinia)     │
                        └──────────────┬───────────────┘
                                HTTPS  │  REST + SSE
                        ┌──────────────▼───────────────┐
   GitHub webhooks ───► │  API service (FastAPI)       │ ◄─── CI (API key)
                        │  auth, CRUD, enqueue, SSE    │
                        └───┬───────────────┬──────────┘
                            │               │
                 ┌──────────▼───┐     ┌─────▼──────┐
                 │ PostgreSQL   │     │ Redis      │
                 │ data +       │     │ queue,     │
                 │ checkpoints  │     │ locks,     │
                 └──────────▲───┘     │ pub/sub    │
                            │         └─────▲──────┘
                        ┌───┴───────────────┴──────────┐
        OpenAI API ◄─── │  Worker (arq)                │
                        │  LangGraph agent, MCP client │
                        │  reports, notifications      │
                        └──────────────┬───────────────┘
                 internal HTTP + MCP   │   (per-job signed token)
   ════════════════ trust boundary ════╪══════════════════════════
                        ┌──────────────▼───────────────┐
                        │  Runner (hardened container) │
                        │  ├ Test engine (Playwright)  │
                        │  └ Playwright MCP server(s)  │
                        └───────┬──────────────┬───────┘
                                │              │ write-only
                        ┌───────▼───────┐  ┌───▼────────────┐
                        │ Egress proxy  │  │ Evidence store │
                        │ (allowlist)   │  │ (volume / S3)  │
                        └───────┬───────┘  └────────────────┘
                                ▼
                     Application under test
```

### 3.1 Services

| Service | Responsibility | Has access to |
|---------|----------------|---------------|
| `frontend` | SPA served as static files. | API only. |
| `api` | Authentication, authorization, CRUD, validation, job enqueue, SSE fan-out, webhook intake, evidence download. | Postgres, Redis, evidence store (read). |
| `worker` | Runs the LangGraph workflow, calls OpenAI, drives the runner, generates reports, sends notifications, calls GitHub. | Postgres, Redis, OpenAI, GitHub, runner, evidence store (read). |
| `runner` | Runs browsers. Hosts the deterministic test engine and per-execution Playwright MCP server processes. | Egress proxy, evidence store (write). **No database, no OpenAI key, no platform secrets.** |
| `egress-proxy` | The only route from the runner to any network. Enforces the per-execution origin allowlist. | Target applications. |
| `postgres` | System of record and LangGraph checkpoint store. | — |
| `redis` | Job queue, distributed locks, cancellation flags, rate limits, live-event pub/sub. | — |

The runner is deliberately separated from the worker. The browser processes untrusted web content, so it lives in the container that has nothing worth stealing except the test credentials of the one job it is running.

### 3.2 The hybrid testing model

Three components, three distinct jobs:

| Component | Job | Decides pass/fail? |
|-----------|-----|--------------------|
| **Deterministic test engine** (Playwright Python inside the runner) | Executes stored test steps with locators, auto-waiting and assertions. Captures evidence. | **Yes — the only component that does.** |
| **Playwright MCP** | Gives the agent browser tools for exploration: discovering the real DOM before generating cases, reproducing a failure, proposing replacement locators. | No. |
| **LLM (OpenAI via LangChain)** | Understands requirements, produces structured plans/cases, classifies and explains failures, proposes follow-up cases. | No. May only leave a verdict unchanged or downgrade it to `inconclusive`. |

LangGraph orchestrates the three. See [agent-workflow.md](agent-workflow.md).

### 3.3 Key design decisions

| # | Decision | Reason |
|---|----------|--------|
| D1 | Test cases are stored as a typed JSON step DSL, not as generated code. | No LLM-authored code is ever executed; steps are schema-validated, diffable, editable in the UI, and replayable without an LLM. |
| D2 | The engine is Playwright **Python** (async API) with our own step interpreter, not Playwright Test (TypeScript). | One backend language; the DSL models are shared between API, worker and runner. Export to Playwright Test spec files is offered as a one-way export in Phase 11. |
| D3 | One Playwright MCP server process per execution, started by the runner with `--isolated`. | Session isolation between executions and projects. |
| D4 | Login is performed by the deterministic engine from a stored login recipe; the resulting storage state is handed to the MCP session. | The LLM never sees or types a password. |
| D5 | arq for background jobs on Redis. | Async-native (LangGraph, Playwright and the MCP client are all async), Redis-only, small. Wrapped in a `JobQueue` interface so it can be swapped. |
| D6 | SSE for live progress, fed by Redis pub/sub. | One-way stream; simpler than WebSockets and proxy-friendly. |
| D7 | LangGraph checkpoints in Postgres (`langgraph-checkpoint-postgres`). | Human-approval interrupts and crash recovery need durable graph state; no new datastore. |
| D8 | Evidence on a filesystem volume behind an `EvidenceStore` interface, with an S3-compatible backend as a deployment option. | Object storage is not in the mandated stack; the interface keeps it optional. |
| D9 | A filtering forward proxy is the only addition to the mandated stack. | URL allowlisting must be a network boundary. Playwright MCP's own origin options are documented as not being a security boundary. |
| D10 | No MongoDB, no vector database. | Nothing in scope needs them. |

## 4. Backend architecture

Layered FastAPI application; dependencies point inward.

```
backend/
  app/
    main.py                 # app factory, middleware, routers
    core/                   # config (pydantic-settings), security, logging, errors, db, redis
    api/v1/                 # routers + dependencies (auth, tenant, pagination)
    schemas/                # Pydantic request/response models
    models/                 # SQLAlchemy 2.0 ORM models
    repositories/           # tenant-scoped data access
    services/               # business logic (one module per domain)
    agent/                  # LangGraph: state, graph, nodes, prompts, llm, budgets
    mcp/                    # MCP client, tool policy, session handle
    runner_client/          # HTTP client for the runner service
    evidence/               # EvidenceStore interface + filesystem / S3 backends
    integrations/github/    # GitHub App client, webhook handlers, check runs
    notifications/          # channels + dispatcher
    reports/                # report builders (JSON, HTML, JUnit)
    worker/                 # arq settings and job functions
  alembic/
  tests/
packages/
  testdsl/                  # shared Pydantic models: steps, locators, assertions, results
runner/
  runner_app/               # internal HTTP app, engine, MCP process manager, redaction
frontend/
deploy/                     # compose files, proxy config
fixtures/demo-app/          # seeded target app with intentional defects (for tests/evals)
docs/
```

Rules:

- Routers do validation and authorization only; logic lives in services.
- Repositories take a `TenantContext` and always filter by `organization_id`.
- All I/O is async (SQLAlchemy async engine with asyncpg, redis-py asyncio).
- Settings come from environment variables through one `Settings` class. No secret has a default value.
- Errors are raised as typed domain exceptions and mapped to one error envelope (see [api-design.md](api-design.md)).

## 5. Frontend architecture

Vue 3 + TypeScript + Vite + Pinia, with Vue Router (required for a multi-page SPA).

```
frontend/src/
  api/            # typed fetch client; types generated from the OpenAPI schema
  router/         # routes + auth/role guards
  stores/         # Pinia stores (auth, org, projects, instructions, cases, executions, ...)
  composables/    # useEventStream, usePagination, usePermission, useAsync
  components/
    ui/           # Button, Input, Select, Modal, Table, Tabs, Badge, Toast, ...
    domain/       # StatusBadge, StepList, LocatorView, EvidenceViewer, VerdictSummary, ...
  layouts/        # AuthLayout, AppLayout
  pages/          # one folder per feature area
  styles/         # design tokens (CSS variables), base styles
```

### 5.1 Pages

| Area | Route | Purpose |
|------|-------|---------|
| Auth | `/login`, `/register`, `/accept-invite` | Sign in, sign up, join an organization. |
| Dashboard | `/` | Pass rate trend, recent executions, pending approvals, flaky cases, LLM spend. |
| Projects | `/projects`, `/projects/:id` | List, create, project overview. |
| Applications | `/projects/:id/applications/:appId` | Application details and its environments. |
| Environments | `.../environments/:envId` | Base URL, allowed origins, kind, read-only flag, credentials. |
| Instructions | `/projects/:id/instructions`, `.../:instructionId` | Submit an instruction; view analysis, clarifying questions, plan. |
| Generated cases | `.../instructions/:instructionId/review` | Review, edit, approve or reject AI-generated cases. |
| Test cases | `/projects/:id/cases`, `.../:caseId` | Case list, step editor, version history. |
| Suites | `/projects/:id/suites`, `.../:suiteId` | Group cases, run a suite. |
| Executions | `/projects/:id/executions` | History with filters. |
| Live execution | `/executions/:id` | Live step progress, current screenshot, cancel. |
| Results | `/executions/:id/results/:resultId` | Step timeline, expected vs actual, failure analysis, heal proposals. |
| Evidence | (panel within results) | Screenshot gallery, video, trace download, console/network logs. |
| Reports | `/projects/:id/reports`, `/reports/:id` | Report list and viewer; export. |
| Integrations | `/settings/integrations` | GitHub App install, webhooks, CI API keys. |
| MCP | `/settings/mcp` | MCP connections, tool allowlist, health. |
| Settings | `/settings/*` | Profile, organization, members and roles, limits, notifications. |
| Audit logs | `/settings/audit` | Filterable audit trail (admin only). |

### 5.2 State management

- One Pinia store per domain. Stores hold normalized entities keyed by ID plus list/query state.
- `auth` store holds the access token in memory only; the refresh token is an HttpOnly cookie.
- `executions` store merges REST snapshots with SSE events through one reducer so a reconnect cannot produce a divergent view.
- Server data is not duplicated into component state; components read from stores.

### 5.3 Reusable components

`ui/` components are unstyled-logic-free primitives built in-house on CSS variables. No UI kit is added unless approved (see open questions in [risks-and-limitations.md](risks-and-limitations.md)).

## 6. Test case generation strategy

1. **Analyse** the instruction into a structured `RequirementAnalysis` (feature, actors, flows, inputs, expected outcomes, test types, ambiguities).
2. **Ground** the analysis in the real application: a bounded MCP exploration captures accessibility snapshots of the relevant pages so that generated locators refer to elements that exist.
3. **Plan**: scope, test types, viewports, data strategy, credential roles.
4. **Generate** cases using standard design techniques — happy path, negative, boundary values, equivalence classes, state transitions, role/permission checks.
5. **Validate** every case with deterministic rules (schema, locator policy, URL scope, at least one assertion, oracle provenance, side-effect flags).
6. **Review**: a human approves before cases become runnable, unless project policy allows auto-approval for non-production environments.

**Oracle provenance.** Every expected value is tagged with its source: `user_specified`, `derived_rule`, `dom_constraint`, or `llm_inferred`. An `llm_inferred` expected value cannot produce a `passed` verdict until a human has approved the case version. This matters most for calculations such as loyalty points: the user configures the rule (for example "1 point per ₹100, rounded down"), a safe arithmetic evaluator computes expected values for each bill amount, and the LLM only proposes which amounts to test.

## 7. Test execution and verification strategy

- The engine interprets the step DSL using Playwright locators and web-first assertions (auto-waiting).
- Each case × dataset row × viewport × browser is one **result**.
- Verdict per result: `passed`, `failed`, `flaky`, `error`, `skipped`, `blocked`, `inconclusive`.
  - `passed`: every assertion step passed on the first attempt.
  - `flaky`: failed, then passed on the permitted retry. Reported separately; never folded into `passed`.
  - `failed`: a deterministic assertion failed reproducibly.
  - `error`: the test could not be evaluated (infrastructure, locator not found, environment down).
  - `inconclusive`: the case has no verified oracle, or evidence is contradictory.
- API testing uses Playwright's `APIRequestContext` through the same egress proxy.
- Responsive testing runs the same case across a viewport matrix and adds layout assertions (no horizontal overflow, target visibility, minimum tap-target size).
- Audit mode ("find broken buttons") is a bounded, deterministic crawl that records findings — console errors, failed requests, controls with no observable effect. Findings are reported with a detection type (`deterministic` or `heuristic`) and are not pass/fail verdicts.

## 8. Evidence management

| Evidence | When captured |
|----------|---------------|
| Screenshot | On every failed step; on configured checkpoints; final state of each result. |
| Video | Per result when enabled (default: retained on failure only). |
| Playwright trace | Per result on first retry or failure. |
| Console log, network log | Always collected; stored on failure or when findings exist. |
| DOM / accessibility snapshot | On failed steps (input to failure analysis and healing). |
| API exchange | Request and response for API steps, with secrets redacted. |

The runner writes evidence to a per-execution directory and returns metadata (key, hash, size, type). The worker registers it in `test_evidence`. Redaction runs in the runner before anything is persisted (see [security-design.md](security-design.md)). Downloads go through the API with authorization and short-lived signed URLs. Retention is per organization, enforced by a scheduled cleanup job.

## 9. Reporting architecture

- **Source of truth** is the relational data (`test_executions`, `test_results`, `test_execution_steps`, `test_findings`, `test_evidence`).
- A report is a rendered snapshot: `summary` JSON stored in `test_reports`, plus HTML and JUnit XML files in the evidence store.
- Report content: verdict totals, per-case results, step timelines with expected vs actual, evidence links, findings, failure analysis (labelled as AI-generated and advisory), environment and git context, LLM usage.
- The narrative summary is written by the LLM from the structured results. It is given the verdicts as fixed input and cannot change them; a post-check verifies the counts quoted in the narrative match the data.
- Dashboard analytics are SQL aggregates over results, cached briefly in Redis.

## 10. Monitoring, logging and evaluation

- **Logging**: JSON to stdout with `request_id`, `organization_id`, `execution_id`, `thread_id`. A redaction filter removes secrets.
- **Agent trace**: every node transition, LLM call (model, tokens, cost, latency) and tool call (name, redacted arguments, outcome) is written to `agent_execution_logs`.
- **Health**: `/healthz` (liveness) and `/readyz` (Postgres, Redis, runner reachability) on each service.
- **Operational metrics** exposed through the dashboard API: queue depth, execution duration, runner utilisation, failure-class distribution, flaky rate, cost per execution.
- **Evaluation**: a golden set of instructions run against `fixtures/demo-app` (which has known, seeded defects). Tracked metrics: schema-valid generation rate, scenario coverage against a reference list, defect detection rate, false-fail rate, false-pass rate (must be zero), heal precision, cost and duration. See [testing-strategy.md](testing-strategy.md).

External tracing (LangSmith, OpenTelemetry) is not included by default because it would send data to another service; it is listed as an open decision.

## 11. Cross-references

| Topic | Document |
|-------|----------|
| Schema | [database-design.md](database-design.md) |
| Endpoints | [api-design.md](api-design.md) |
| LangGraph state, nodes, edges, retries | [agent-workflow.md](agent-workflow.md) |
| MCP client/server responsibilities, sessions | [playwright-mcp-integration.md](playwright-mcp-integration.md) |
| Authentication, isolation, SSRF, prompt injection | [security-design.md](security-design.md) |
| How the platform itself is tested | [testing-strategy.md](testing-strategy.md) |
| Compose topology, CI/CD | [deployment.md](deployment.md) |
| Phases | [implementation-plan.md](implementation-plan.md) |
| Gaps and risks | [risks-and-limitations.md](risks-and-limitations.md) |
