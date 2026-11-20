# Implementation Plan

Fourteen phases, in the order requested. Each phase ends in a working, tested increment. Status and deviations are recorded in [progress.md](progress.md).

## How to read this plan

- **Frontend is built incrementally.** Each phase adds the minimum screens needed to use what it delivers. Phase 12 completes, unifies and polishes the UI; it is not where the frontend starts.
- **Security controls land with the feature that needs them.** URL validation arrives with environments (Phase 3), runner isolation and the egress proxy with the first browser (Phase 6), and so on. Phase 13 hardens and verifies; it does not introduce the first line of defence.
- **Every phase's completion criteria include**: CI green (Ruff, mypy, Pytest, ESLint, `vue-tsc`, Vitest), migrations reviewed, docs updated.

## Dependency overview

```
1 Foundation ─► 2 DB + Auth ─► 3 Projects/Apps/Envs ─► 4 Requirement analysis ─► 5 Case generation
                                        │                                              │
                                        └────────────► 6 MCP integration ◄─────────────┤ (grounding)
                                                        │                              │
                                                        └─► 7 Test engine ─────────────┤
                                                                    │                  ▼
                                                                    └──────► 8 LangGraph workflow
                                                                                  │
                                              9 Validation + healing ◄────────────┤
                                             10 Reports + evidence  ◄─────────────┤
                                             11 GitHub + CI/CD      ◄──── 10
                                             12 Frontend dashboard  ◄──── all APIs
                                             13 Security + reliability
                                             14 Testing, evaluation, deployment
```

Phases 6 and 7 share the runner container and can be built in parallel by two people. Phases 4 and 5 implement their nodes as standalone, individually tested functions; Phase 8 assembles them into the full graph.

---

## Phase 1 — Project Foundation

| | |
|---|---|
| **Objectives** | A repository where every later phase can start writing features immediately: structure, tooling, containers, CI. |
| **Features** | Monorepo layout (`backend/`, `runner/`, `frontend/`, `packages/testdsl/`, `deploy/`, `fixtures/demo-app/`, `docs/`). Health endpoints. Typed settings. Structured logging with request IDs. Error envelope. |
| **Backend** | FastAPI app factory; `core/` (config, logging, errors, db session, redis client); arq worker skeleton with a no-op job; Alembic initialised (async); `/healthz`, `/readyz`. |
| **Frontend** | Vite + Vue 3 + TypeScript + Pinia + Vue Router scaffold; design tokens; first UI primitives (Button, Input, Modal, Toast); typed API client generated from OpenAPI; app shell layout. |
| **Database** | Empty baseline migration; roles (application, migration); extensions (`citext`). |
| **APIs** | `GET /healthz`, `GET /readyz`. |
| **Dependencies** | None. Decisions needed: egress proxy option, UI component approach (see [risks-and-limitations.md](risks-and-limitations.md) §1). |
| **Security** | No secrets in the repository; `.env.example` with names only; secret scanning and dependency audit in CI; non-root images. |
| **Test cases** | Health endpoints respond; settings fail fast when a required secret is missing; error envelope shape; log lines contain the request ID; frontend smoke render; CI pipeline runs all linters and test runners. |
| **Completion criteria** | `docker compose up` starts api, worker, postgres, redis, frontend; CI green on an empty feature set; `fixtures/demo-app` skeleton serves a page. |
| **Risks** | Over-engineering the scaffold. Mitigation: only what Phase 2 needs. |

## Phase 2 — Database and Authentication

| | |
|---|---|
| **Objectives** | Users, organizations, roles, sessions, API keys, tenant isolation, audit logging. |
| **Features** | Register, login, refresh rotation, logout, password change/reset, invitations, role management, API keys, audit trail. |
| **Backend** | ORM base with tenant mixin; `TenantContext` and tenant-scoped repository base; auth service (Argon2id, JWT, refresh rotation with reuse detection); RBAC dependencies; rate limiting in Redis; audit service. |
| **Frontend** | Login, registration, accept-invite pages; auth store with silent refresh; route guards; organization switcher; members page (basic). |
| **Database** | `users`, `organizations`, `organization_members`, `refresh_tokens`, `api_keys`, `audit_logs` (partitioned). RLS policies and the `app.current_org` session setting. |
| **APIs** | `/auth/*`, `/organizations/*`, members, invitations, API keys, `/users/me`. |
| **Dependencies** | Phase 1. |
| **Security** | Everything in [security-design.md](security-design.md) §3–4; login throttling; identical errors for unknown user and wrong password; refresh cookie flags; CORS allowlist. |
| **Test cases** | Token rotation and reuse revokes the family; lockout after repeated failures; role matrix per endpoint; cross-tenant access returns 404; RLS blocks a query without the tenant setting; last owner cannot be removed; API key shown once, hash stored; audit rows written for each sensitive action. |
| **Completion criteria** | Two organizations can be created and are provably isolated by an automated test that sweeps every tenant table and endpoint. |
| **Risks** | RLS with async sessions and connection pooling (setting must be transaction-local). Mitigation: `SET LOCAL` in a session event hook, with a test that reuses pooled connections across tenants. |

## Phase 3 — Project and Application Management

| | |
|---|---|
| **Objectives** | Define what may be tested: projects, applications, environments, allowed origins, credentials. |
| **Features** | CRUD for all four; origin allowlist editor; encrypted credential storage with login recipes; environment kind and read-only flags. |
| **Backend** | Services and repositories; URL/origin validator (scheme, host, IP-range rules, DNS resolution); envelope encryption service with versioned keys; SSRF-safe outbound HTTP client. |
| **Frontend** | Projects list/detail; application page; environment form with origins; credential form (write-only secret fields); login-recipe editor (first use of the step editor component). |
| **Database** | `projects`, `applications`, `environments`, `environment_allowed_origins`, `credentials`. |
| **APIs** | Section 4 of [api-design.md](api-design.md), except `verify` and `test-login`, which need the runner and are delivered in Phase 7. |
| **Dependencies** | Phase 2. |
| **Security** | Private/reserved address rejection; admin-only allowlist and credential management; secrets never returned; associated-data binding of ciphertext to tenant; audit of every change. |
| **Test cases** | Validator matrix (loopback, private IPv4/IPv6, mapped addresses, metadata address, userinfo, odd ports, punycode); `base_url` must match an allowed origin; production defaults to read-only and approval-required; encryption round trip and rotation; credential response contains no secret; soft-delete uniqueness. |
| **Completion criteria** | An admin can configure the demo app as an application with a staging environment and a customer credential; no API response or log line contains the secret. |
| **Risks** | Users need to test internal staging hosts on private addresses. Mitigation: explicit, audited `allow_private_network` per environment. |

## Phase 4 — AI Requirement Analysis

| | |
|---|---|
| **Objectives** | Turn a natural-language instruction into a validated, structured requirement analysis, asking for clarification when needed. |
| **Features** | Submit instruction; asynchronous analysis; clarification questions and answers; LLM usage tracking and budgets. |
| **Backend** | LLM gateway (`langchain-openai`, per-role model config, structured output with strict schema, retry/backoff, one repair attempt, cost accounting, budget enforcement); prompt files with versions; `analyse_requirement` and clarification nodes as standalone functions; job to run them; fake LLM for tests. |
| **Frontend** | Instruction submission form (application, environment, types, options, rules); instruction detail with analysis view; clarification dialog. |
| **Database** | `test_instructions`, `agent_execution_logs` (partitioned). |
| **APIs** | Test instruction endpoints: create, list, get, clarifications, cancel. |
| **Dependencies** | Phase 3. OpenAI account and model selection. |
| **Security** | Instruction length and character limits; no credentials or environment secrets in prompts; per-organization rate limit and LLM budget; data-sharing notice. |
| **Test cases** | Each example instruction from the brief yields a schema-valid analysis (recorded responses); ambiguous instruction (loyalty points without a rule) produces a clarification; clear instruction does not; malformed LLM output triggers one repair then fails cleanly; budget exhaustion returns `budget_exceeded`; provider 429/5xx are retried with backoff; every call is logged with tokens and cost. |
| **Completion criteria** | Submitting any of the brief's example instructions produces a stored analysis or a clarification request, with usage recorded. |
| **Risks** | Model/SDK behaviour differences in structured output. Mitigation: isolate in the gateway; recorded-response tests. |

## Phase 5 — AI Test Case Generation

| | |
|---|---|
| **Objectives** | Produce plans and executable, validated test cases in the step DSL; let humans review and edit them. |
| **Features** | Step DSL package; plan generation; scenario generation (happy, negative, boundary, permission, data-driven); deterministic validator; case versioning; suites; review and approval of generated cases. |
| **Backend** | `packages/testdsl` (steps, locators, assertions, datasets, placeholders); safe expression evaluator for configured rules; `generate_plan` (without exploration yet), `generate_scenarios`, `validate_scenarios` nodes; case/suite services; approvals service. |
| **Frontend** | Plan view; generated-cases review page (accept/reject/edit, validation issues inline); step editor; test case list/detail with version history; suites. |
| **Database** | `test_plans`, `test_suites`, `test_cases`, `test_case_versions`, `test_suite_cases`, `test_steps`, `approvals`. |
| **APIs** | Plan, generated cases, regenerate; test case and suite endpoints; bulk review; validate. |
| **Dependencies** | Phase 4. |
| **Security** | Validator rejects off-scope URLs, secret-like literals, disallowed locators, mutating steps on read-only environments; oracle provenance recorded; approved versions immutable. |
| **Test cases** | One passing and one failing unit test per validator rule; duplicate detection; regeneration cap of two; datasets for loyalty points use the configured rule and are tagged `derived_rule`; without a rule, expected values are `llm_inferred` and the case needs approval; editing an approved case creates a new version; expression evaluator rejects anything outside the whitelist. |
| **Completion criteria** | For the demo app, each example instruction yields reviewable cases that pass validation; a human can edit, approve and group them into a suite. Cases are not yet executable. |
| **Risks** | Without seeing the application, generated locators are guesses. Accepted for this phase: cases are flagged ungrounded until Phase 6 adds exploration. |

## Phase 6 — Playwright MCP Integration

| | |
|---|---|
| **Objectives** | A hardened runner that can host isolated browser sessions, and an MCP client the agent can use to explore within scope. |
| **Features** | Runner service; session lifecycle; egress proxy with per-session allowlists; managed Playwright MCP per session; tool discovery and allowlist; MCP connection management; grounded exploration for plan generation. |
| **Backend** | Runner app (`/sessions`, MCP start, reverse proxy, cancel, delete, health); `McpProcessManager`; egress proxy; worker-side `RunnerClient` and `McpClient` wrapper (allowlist, argument validation, truncation, redaction, logging, budgets); exploration sub-step wired into `generate_plan`; health-check job. |
| **Frontend** | MCP settings page: connections, discovered tools, allowlist toggles, health status. Exploration summary shown on the plan view. |
| **Database** | `mcp_connections`; `discovered_tools` and `server_version` populated by health checks. |
| **APIs** | MCP connection endpoints; internal runner API (session and MCP parts). |
| **Dependencies** | Phases 3 and 5. Verification of the pinned `@playwright/mcp` version's tools and flags ([playwright-mcp-integration.md](playwright-mcp-integration.md) §11). |
| **Security** | Runner hardening per [security-design.md](security-design.md) §7; internal network; signed session tokens; default tool policy; observe-only mode on read-only environments; external MCP mode admin-only. |
| **Test cases** | Contract test for discovered tools; blocked tools are never bound; off-scope navigation rejected by client and by proxy independently; DNS-rebinding simulation at the proxy; runner cannot reach Postgres/Redis/internet; two sessions share nothing; server crash → reconnect → degraded path; tool and time budgets end the loop; snapshot truncation keeps output under the token budget. |
| **Completion criteria** | An exploration of the demo app's login and product pages returns an element inventory; generated locators are checked against it; all isolation tests pass. Session login is anonymous until Phase 7 supplies the login recipe executor. |
| **Risks** | MCP server API churn; proxy correctness; snapshot size. Mitigations: pinned version and contract tests; focused proxy test suite; truncation strategy tested on real pages. |

## Phase 7 — Playwright Test Engine

| | |
|---|---|
| **Objectives** | Deterministically execute stored cases and produce step-level outcomes and evidence. |
| **Features** | Step interpreter for the full DSL; locator resolution with strictness and fallbacks; assertions; login recipe execution and storage-state handoff (also to MCP); viewport matrix; API steps; audit crawl; evidence capture with masking; execution records; manual run of a single case or suite; cancellation. |
| **Backend** | Engine in the runner (`/sessions/{id}/run` streaming events); context routing (allowlist, read-only method block); evidence writer; worker job that runs work items sequentially and persists results (a simple linear runner, replaced by the graph in Phase 8); verdict calculator; `EvidenceStore` (filesystem). |
| **Frontend** | "Run" action on cases and suites; execution list; basic execution detail with step outcomes and screenshots. |
| **Database** | `test_executions`, `test_results`, `test_execution_steps`, `test_evidence`, `test_findings`. |
| **APIs** | Execution create/list/get/cancel; results; findings; evidence list/content; environment `verify`; credential `test-login`. |
| **Dependencies** | Phases 5 and 6. |
| **Security** | Credentials substituted only in runner memory; password masking in screenshots/traces; uploads only from the fixture store; read-only enforcement; per-session resource limits; signed evidence URLs. |
| **Test cases** | Every action and assertion, pass and fail, against the demo app; ambiguous locator is an error; fallback use is recorded; seeded defects are detected (dead button, missing validation, rounding error, overflow at 375 px, API 500); verdict rules; cancellation mid-case; evidence present and masked for failures; credential value absent from all artefacts. |
| **Completion criteria** | An approved suite for the demo app runs end to end without any LLM call and reports correct verdicts for all seeded defects and known-good behaviours. |
| **Risks** | Flakiness from timing; evidence volume growth. Mitigations: web-first assertions only, no sleeps; video on failure only; size caps. |

## Phase 8 — LangGraph Agent Workflow

| | |
|---|---|
| **Objectives** | Assemble the full workflow with durable state, approvals, retries, budgets, cancellation and live progress. |
| **Features** | The graph in [agent-workflow.md](agent-workflow.md): three run modes; Postgres checkpointing; interrupts for clarification and approval; retry decision node; live events over SSE; concurrency limits; reconciler for stuck runs. |
| **Backend** | `AgentState`; nodes 1–14 (reusing Phase 4/5 functions and the Phase 7 runner client); conditional edges as pure functions; checkpointer setup; resume jobs; Redis locks, cancellation flags and event publishing; SSE endpoint with tickets; organization concurrency semaphore. |
| **Frontend** | Live execution page (node progress, step stream, current screenshot, cancel); approval inbox and decision dialog; `useEventStream` composable with reconnect. |
| **Database** | LangGraph checkpoint schema; `graph_thread_id` columns in use; approval reason codes. |
| **APIs** | `mode` on instructions; approvals endpoints; stream ticket and events; rerun. |
| **Dependencies** | Phases 4–7. |
| **Security** | Approval rules (production, destructive steps, inferred oracles); approver role and segregation checks; approval void if the payload changes; budgets enforced centrally. |
| **Test cases** | With fake LLM and fake runner: happy path per mode; clarification round trip; approval approve/reject/expire; regeneration loop cap; retry rounds cap; budget exhaustion; cancellation at every node; worker crash and resume from checkpoint; no duplicate results after resume; SSE ordering and resume with `Last-Event-ID`. |
| **Completion criteria** | "Test the customer login functionality" against the demo app runs from instruction to stored results through the graph, pausing for approval, and survives a worker restart mid-run. |
| **Risks** | Non-idempotent side effects on resume; state growth. Mitigations: upserts on natural keys; IDs and summaries only in state. |

## Phase 9 — Test Validation and Self-Healing

| | |
|---|---|
| **Objectives** | Trustworthy outcomes: correct classification, honest flaky reporting, safe locator healing, useful failure explanations. |
| **Features** | Deterministic failure classification; retry table; `flaky` status; LLM failure analysis (advisory); bounded MCP failure investigation; heal proposals verified by deterministic re-run; accept/reject flow creating new case versions; suggestions for additional cases. |
| **Backend** | `validate_results`, `analyse_failures`, `decide_retry` completed; heal verifier; proposal service; structural guard that makes it impossible for analysis output to reach verdict code (separate types, no shared write path). |
| **Frontend** | Result detail: expected vs actual, failure class, AI analysis panel labelled advisory; heal proposal review (old/new locator, screenshot); flaky indicators; case history. |
| **Database** | `locator_heal_proposals`; `failure_class`, `ai_analysis`, `healed` populated. |
| **APIs** | Heal proposal list and decision; case history; result detail extended. |
| **Dependencies** | Phase 8. |
| **Security** | Healing limited to locators; proposals require human acceptance; investigation uses the same tool policy and budgets as exploration. |
| **Test cases** | Rename a button in the demo app → locator failure → proposal → verified re-run → `healed` flag → acceptance creates a new version; proposal resolving to the wrong role is rejected; assertion failure is never healed; pass on retry is `flaky`; fake LLM asserting "passed" changes nothing; unapproved inferred oracle yields `inconclusive`; environment-down marks remaining items `blocked`. |
| **Completion criteria** | On the evaluation fixture: zero false passes, zero LLM-originated passes, all retry and healing rules covered by tests. |
| **Risks** | A heal that picks a plausible but wrong element. Mitigations: uniqueness and role checks, full deterministic re-run, human acceptance, heal precision metric. |

## Phase 10 — Reports and Evidence Management

| | |
|---|---|
| **Objectives** | Complete, shareable reports and managed evidence. |
| **Features** | JSON, HTML and JUnit reports; narrative summary with count verification; evidence viewer; retention cleanup; redaction pipeline with quarantine; share links; notifications (in-app and outbound webhook; email if confirmed); analytics endpoints. |
| **Backend** | Report builders; `generate_report` and `store_history` completed; retention and partition jobs; redaction service; notification dispatcher with retry; analytics queries with caching; optional S3 evidence backend. |
| **Frontend** | Report viewer and list; evidence gallery (screenshots, video, logs, trace download); notification centre and preferences. |
| **Database** | `test_reports`, `notifications`; retention fields in use. |
| **APIs** | Reports, share, evidence, notifications, analytics. |
| **Dependencies** | Phases 8–9. Decision on notification channels and retention values. |
| **Security** | HTML reports escaped and served under a strict CSP; signed, short-lived evidence URLs; quarantined artefacts admin-only; share links expirable, revocable, audit-logged; webhooks signed and SSRF-safe. |
| **Test cases** | Report totals equal database totals; narrative with wrong counts is dropped; JUnit validates against the schema and maps statuses correctly; page text with script tags is escaped; expired evidence is deleted from store and database; another tenant cannot fetch evidence by ID or signed URL; notification retry and failure states. |
| **Completion criteria** | Every finished execution has JSON, HTML and JUnit reports with working evidence links; retention job verified. |
| **Risks** | Storage growth; imperfect redaction of unstructured content. Mitigations: caps, retention, mask selectors, quarantine. |

## Phase 11 — GitHub and CI/CD Integration

| | |
|---|---|
| **Objectives** | Run tests from pull requests and pipelines and publish results where developers work. |
| **Features** | GitHub App installation; repository mappings; webhook triggers (`pull_request`, `deployment_status`); check runs and PR comments; issue creation from failures; CI endpoints; `qa-ci` client and example workflow; optional one-way export of cases to Playwright Test spec files. |
| **Backend** | GitHub client (App JWT, installation tokens); webhook receiver with signature verification and deduplication; trigger service; check-run publisher job; CI router; exporter. |
| **Frontend** | Integrations page: install GitHub App, map repositories, choose triggers; API key management; git context shown on executions. |
| **Database** | `integrations`; `git_context` and `api_key_id` on executions in use. |
| **APIs** | GitHub integration endpoints, `/webhooks/github`, `/ci/*`. |
| **Dependencies** | Phase 10 (reports, JUnit). A registered GitHub App. |
| **Security** | Minimal App permissions; webhook HMAC and replay protection; signed single-use install `state`; API keys cannot approve; page-derived text escaped in comments and issues; forks do not trigger runs with credentials unless a maintainer approves. |
| **Test cases** | Signature valid/invalid/replayed; PR event starts exactly one execution per SHA (idempotency); check run reflects verdict including `flaky` and `awaiting_approval`; unmapped repository ignored; CI client exit codes per verdict; API key scope enforcement. |
| **Completion criteria** | Opening a pull request on a mapped test repository runs the mapped suite and publishes a check run and comment linking to the report. |
| **Risks** | Preview environments have dynamic URLs that are not on the allowlist. Mitigation: `include_subdomains` entries or a pattern-registered preview environment — needs a product decision. |

## Phase 12 — Frontend Dashboard

| | |
|---|---|
| **Objectives** | Complete and unify the user interface. |
| **Features** | Dashboard with trends, pass rate, flaky cases, pending approvals, usage; every page in [architecture.md](architecture.md) §5.1 complete; settings (profile, organization, members, limits, notification preferences); audit log viewer; consistent empty, loading and error states; accessibility pass; responsive layout. |
| **Backend** | Gaps found during UI completion only (filters, sort options, aggregate endpoints); `agent-logs` endpoint. |
| **Frontend** | Component library finalised (`ui/` and `domain/`); store normalisation review; role-aware navigation and actions; charts (in-house SVG components unless a chart library is approved); keyboard navigation and focus management. |
| **Database** | Indexes for dashboard queries as measured. |
| **APIs** | Analytics, audit logs, agent logs; small additions as needed. |
| **Dependencies** | Phases 2–11. |
| **Security** | Actions hidden *and* server-enforced by role; no secrets rendered; CSP compatible build; all page-derived content rendered as text. |
| **Test cases** | Vitest for stores, composables and components; end-to-end journeys from [testing-strategy.md](testing-strategy.md) §2.6; role-based visibility; accessibility checks on key pages; SSE reconnect behaviour in the UI. |
| **Completion criteria** | A new user can go from registration to a viewed report without documentation; all end-to-end journeys pass. |
| **Risks** | Scope creep in UI polish. Mitigation: page list is fixed by the architecture document. |

## Phase 13 — Security and Reliability

| | |
|---|---|
| **Objectives** | Verify and harden everything built so far against the threat model; make failure modes boring. |
| **Features** | Security test suite; prompt-injection test pages; secret-leak scans; quotas and rate limits reviewed; key rotation procedure; kill switches; reconcilers; graceful shutdown; backup and restore. |
| **Backend** | Fixes from the security review; credential re-wrap job; global and per-organization feature switches (LLM, MCP, executions); stuck-execution reconciler tuned; worker drain on shutdown; alert hooks. |
| **Frontend** | Admin controls for limits and switches; session expiry handling; security-relevant confirmations (enabling blocked tools, private network, production overrides). |
| **Database** | RLS verified on every tenant table; least-privilege roles verified; missing constraints or indexes found in review. |
| **APIs** | Organization limits and switches; no new public surface otherwise. |
| **Dependencies** | Phases 1–12. |
| **Security** | Full walk through [security-design.md](security-design.md) §15; external review or penetration test recommended before production. |
| **Test cases** | Everything in [testing-strategy.md](testing-strategy.md) §2.7–2.8: cross-tenant sweep, SSRF matrix, injection pages, secret scan, sandbox checks, chaos tests (kill worker, runner, Redis; LLM outage). |
| **Completion criteria** | All security and reliability tests automated and passing; no open high-severity findings; rotation and restore procedures rehearsed. |
| **Risks** | Late discovery of a design-level flaw. Mitigation: the controls were built in their phases; this phase is verification, and the earlier phases' test cases already cover the main boundaries. |

## Phase 14 — Testing, Evaluation and Deployment

| | |
|---|---|
| **Objectives** | Prove quality with numbers and ship. |
| **Features** | Agent evaluation suite and golden set; evaluation reports per prompt/model version; performance tests; production Compose configuration; CD pipeline; operations runbook. |
| **Backend** | Evaluation harness (runs the golden set, scores against the defect manifest and reference scenarios, stores metrics); performance fixes; production settings profile. |
| **Frontend** | Production build configuration; usage and evaluation summaries for admins (optional). |
| **Database** | Production tuning (pool sizes, autovacuum for hot tables); backup configuration. |
| **APIs** | None new. |
| **Dependencies** | Phases 1–13; production host, domain and TLS; OpenAI production key and terms; GitHub App in production mode. |
| **Security** | Production readiness checklist in [deployment.md](deployment.md) §12. |
| **Test cases** | Evaluation gates in [testing-strategy.md](testing-strategy.md) §4.3; load tests; deploy and rollback rehearsal on staging; post-deploy smoke test. |
| **Completion criteria** | Evaluation gates met (zero false passes, zero scope violations); staging soak without stuck executions; production deployed with monitoring and backups; runbook written. |
| **Risks** | Evaluation thresholds not met. Mitigation: evaluation harness is started in Phase 9 and run continuously from then on, so this phase confirms rather than discovers. |

---

## Cross-phase work

| Item | Starts | Notes |
|------|--------|-------|
| `fixtures/demo-app` and its defect manifest | Phase 1 (skeleton), grows through Phase 7 | Needed by engine tests, MCP tests and evaluation. |
| Evaluation harness | Phase 9 | Formalised in Phase 14. |
| Documentation | Every phase | The ten documents in `docs/` are updated when a decision changes. |
| Audit logging | Phase 2 onwards | Each phase adds its own events. |

## Definition of done (every phase)

1. Features meet the completion criteria above.
2. Ruff, mypy, Pytest, ESLint, `vue-tsc`, Vitest all pass in CI.
3. New endpoints have success, validation, authentication, authorization and cross-tenant tests.
4. Migrations upgrade and downgrade cleanly.
5. No secret in code, logs, fixtures or images.
6. Relevant documents updated.
