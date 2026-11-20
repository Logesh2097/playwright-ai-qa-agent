# Implementation Progress

Tracks what has been built against [implementation-plan.md](implementation-plan.md).

| Phase | Status |
|-------|--------|
| 0 — Analysis and architecture | Complete |
| 1 — Project foundation | Complete (2026-11-18) |
| 2 — Database and authentication | Complete (2026-11-18); its sign-in pages were added in Phase 3 |
| 3 — Project and application management | Complete (2026-11-18) |
| 4 — AI requirement analysis | Complete (2026-11-18); not yet run against a live model. Its web pages were added in Phase 5 |
| 5 — AI test case generation | Complete (2026-11-18); not yet run against a live model — see below |
| 6 — Playwright MCP integration | Complete (2026-11-19); verified against a real server and browser. No web pages; agent exploration waits for Phase 8 |
| 7 — Playwright test execution engine | Complete (2026-11-19); verified in real browsers. No web pages; scripts are written by hand until Phase 8 |
| 8 — LangGraph agent workflow | Complete (2026-11-19); verified end to end with real browsers and a scripted model. Not yet run against a live model; no web pages |
| 9 — Result validation, failure analysis and self-healing | Complete (2026-11-19); locator recovery verified in real browsers. Failure analysis not yet run against a live model; no web pages |
| 10 — Test reports and evidence management | Complete (2026-11-19); verified with real browsers and through the running stack. Web pages for reports, history and the dashboard included |
| 11 — GitHub and CI/CD integration | Complete (2026-11-19); the CI route verified through the running stack. Not yet run against GitHub itself; no web pages |
| 12 — Complete frontend dashboard | Complete (2026-11-19); every screen walked through in a real browser at desktop and tablet widths |
| 13 — Security, reliability and production safeguards | Complete (2026-11-19) |
| 14 — Complete testing, evaluation and deployment | Complete (2026-11-20); production configuration rehearsed locally. **Not deployed.** Model-based evaluation run once with Gemini (see the addendum) |

## Phase 1 — Project Foundation

### Delivered

**Backend** (`backend/`)

- FastAPI application factory with lifespan-managed PostgreSQL engine and Redis client.
- Typed settings from environment variables; `DATABASE_URL` and `REDIS_URL` have no defaults and the process fails fast without them; connection strings are excluded from `repr`.
- JSON logging for the API and the worker, with the request ID on every line.
- Request context middleware: validates or generates `X-Request-ID`, writes one access-log line per request, and turns any unhandled exception into a generic 500 envelope.
- CORS from an explicit origin list; wildcard is rejected at startup.
- One error envelope (`application/problem+json`) for domain errors, HTTP errors, validation errors and unexpected failures. Validation errors do not echo submitted values.
- API versioning under `/api/v1` (`GET /api/v1/meta`); `GET /healthz` and `GET /readyz` unversioned. Readiness checks PostgreSQL and Redis concurrently with a timeout and returns 503 with per-dependency status when one is down.
- Layering in place: routers → services → repositories; the readiness logic is in `services/health.py`.
- arq worker with a `ping` job and a health probe.
- Alembic (async) with a baseline migration that enables `citext`.
- Placeholder packages for the AI module (`app/agent`) and test orchestration (`app/testing`).

**Frontend** (`frontend/`)

- Vue 3 + TypeScript + Vite + Pinia + Vue Router.
- Central fetch client that converts every failure into one `ApiError` type and understands the backend's error envelope.
- Global error handling for component errors, unhandled promise rejections and router errors, surfaced as notifications.
- Responsive application shell (sidebar collapses to a drawer on small screens), light and dark themes from design tokens.
- Reusable components: button, loading state, error state, status badge, toast host.
- Overview page showing live API readiness.

**Infrastructure and tooling**

- `docker-compose.yml`: PostgreSQL 16, Redis 7 (AOF), one-shot migration job, API, worker, frontend dev server. Health checks on every long-running service, start-up ordering by health, named volumes for data, two networks (the frontend container cannot reach the data stores), ports bound to localhost.
- Multi-stage Dockerfiles with `dev` and `prod` targets; containers run as non-root.
- `.env.example` templates; `make env` generates a local `.env` with a random database password.
- `Makefile` for every routine task; Ruff, mypy (strict), Pytest, ESLint, Prettier, `vue-tsc`, Vitest configured.
- GitHub Actions workflow (`.github/workflows/ci.yml`).

### Validation results (2026-11-18)

| Check | Result |
|-------|--------|
| `docker compose up --wait` | All services healthy; migration job exited 0 |
| `GET /healthz`, `GET /readyz` | 200; readiness reports PostgreSQL and Redis `ok` |
| Readiness with Redis stopped | 503, Redis reported as `error`, PostgreSQL still `ok`; recovers after restart |
| Frontend dev server | Serves the app; `/api` and `/readyz` proxied to the API |
| Backend: Ruff lint, Ruff format, mypy strict | Pass |
| Backend unit tests | 31 passed |
| Backend integration tests (real PostgreSQL, Redis, worker job round trip) | 4 passed |
| Frontend: ESLint, Prettier, `vue-tsc` | Pass |
| Frontend unit tests | 22 passed |
| Frontend production build | Pass |
| `prod` image targets (backend, frontend) | Build; run as non-root; no dev tooling in the backend image |

Not verified: the GitHub Actions workflow has not run (the project is not yet a Git repository with a remote), and the user interface was checked by build, tests and HTTP responses, not by looking at it in a browser.

### Deviations from the Phase 0 plan

| Plan | What was done | Why |
|------|---------------|-----|
| `runner/`, `packages/testdsl/`, `fixtures/demo-app/` skeletons in Phase 1 | Not created | The Phase 1 brief says not to build future-phase features early. They are created in Phases 5–7, where they are first used. |
| Compose files under `deploy/` | Single `docker-compose.yml` at the repository root | Only a development stack exists so far; production Compose arrives with Phase 14. |
| Frontend API types generated from OpenAPI | Hand-written for the three Phase 1 response types | Generation is introduced in Phase 2 with the first real endpoints. |
| Separate application and migration database roles | One role; `MIGRATION_DATABASE_URL` setting exists and is honoured by Alembic | Role separation is only meaningful once row-level security lands in Phase 2. |
| Health paths `/healthz`, `/readyz` | As planned | — |

### Decisions taken in Phase 1

| Topic | Decision |
|-------|----------|
| Python / Node versions | Python 3.12, Node 22, both only inside containers. |
| Python dependency management | `uv` with a committed `uv.lock`. |
| Worker entry point | `python -m app.worker` rather than the `arq` CLI, so that arq's own log lines are JSON as well. |
| UI components | In-house primitives on CSS variables (open question Q3 defaulted as recommended). |
| Unhandled exceptions | Caught in the request middleware (inside CORS) rather than a FastAPI `Exception` handler, so 500 responses still carry the request ID and CORS headers. |

### Still open from Phase 0

The questions in [risks-and-limitations.md](risks-and-limitations.md) §1 remain open. Q1 (deployment model) and Q11 (SSO/MFA) affect Phase 2; the plan proceeds on the recommended defaults unless changed.

### Carried into Phase 2

- Tenant-scoped repository base and `TenantContext` (the current `BaseRepository` only holds the session).
- OpenAPI-driven frontend type generation.
- Application/migration database role split and row-level security.
- Authentication hook in the frontend API client (`getAccessToken` is already a parameter).

---

## Phase 2 — Database and Authentication

### Delivered

**Database**

- All 31 tables of [database-design.md](database-design.md) as SQLAlchemy models, with foreign keys, unique constraints (partial where rows are soft-deleted), CHECK constraints on every status/kind column, indexes and timestamps. Migration `0002` creates them.
- One transaction per request (commit on return, rollback on exception, committed before the response is sent); repositories flush and never commit.
- Repository pattern: `BaseRepository` and `TenantRepository`, with repositories for users, organizations, memberships, invitations, tokens, the audit log and projects.
- Database errors mapped to the standard error envelope (unique → 409, foreign key → 409, check → 422, lost connection → 503). Driver messages and bound values are logged but never returned, and bound values are hidden from error text.

**Authentication**

- Registration (new organization or by invitation), login, logout, logout-all, current user, refresh with single-use rotation, change password, forgot/reset password.
- Argon2id password hashing; password policy; identical responses for unknown email, wrong password and deactivated account.
- Access token in the response body (15 min); refresh token in an `HttpOnly; Secure; SameSite=Strict` cookie scoped to `/api/v1/auth`; CSRF header required on cookie endpoints.
- Session invalidation is immediate: logout, password change, password reset and refresh-token reuse revoke the session and deny its access tokens through Redis.
- Rate limiting in Redis: lockout after repeated failed logins per email, and per-address limits on login, registration and reset requests.

**Authorization and tenant isolation**

- Roles `owner`, `admin`, `qa_engineer`, `developer`, `viewer`, defined as permission sets and enforced in the backend; roles are read from the database on each request.
- Organization APIs: list, create, rename, delete, members, invitations, role changes, removal, audit log.
- Three isolation layers: membership check in the API (404 for outsiders), tenant-scoped repositories, and PostgreSQL row-level security on every tenant table, with the API connecting as a role that cannot bypass it. Composite foreign keys stop a row from referencing another organization's parent.
- Audit log of authentication and membership events, append-only for the application role, with secrets redacted.

### Validation results (2026-11-18)

| Check | Result |
|-------|--------|
| Backend unit tests (no services needed) | 67 passed |
| Backend integration tests (scratch database, real Redis, worker) | 87 passed — 154 in total |
| Ruff lint and format, mypy strict | Pass |
| Migration `0001 → 0002`, downgrade to empty, upgrade again | Pass |
| `alembic check` (models match the migrated schema) | No differences |
| Live smoke test through the running stack: register, me, organizations, refresh, logout, access after logout | 201, 200, 200, 200, 204, 401 |
| Search of API and worker logs for the smoke-test password | No occurrences |
| Frontend lint, type check, tests (unchanged apart from proxy forwarding) | Pass |

What the tests cover, against the brief:

| Asked for | Covered by |
|-----------|-----------|
| Registration, login, invalid credentials | `tests/integration/test_auth.py` |
| Token expiration, refresh token | expired/forged/garbage access tokens; rotation; reuse ends the session; expired refresh token |
| Role permissions | every role against rename, invite, audit log, delete; escalation attempts; last-owner rule |
| Cross-tenant access prevention | every organization route as an outsider → 404; foreign IDs inside own URLs; unfiltered SQL under row-level security; writes into another tenant rejected by the database |
| Database constraints | uniqueness, CHECKs, foreign keys, composite tenant keys, partial unique indexes |
| Migration execution | scratch database built by the real migrations; round trip; drift check |

Not verified: the GitHub Actions workflow has still not run (no Git repository or remote yet).

### Deviations from the plan

| Plan | What was done | Why |
|------|---------------|-----|
| Phase 2 creates only identity tables | All tables created now | The Phase 2 brief asks for the full set of core models. Later phases add behaviour, not tables. |
| Roles `qa_lead`, `tester` | `qa_engineer`, `developer` | The Phase 2 brief names these roles. Documents updated. |
| Frontend: login, registration, auth store, members page | Not built | The Phase 2 brief covers backend only. The web app still has no sign-in; this is owed in the frontend phase (12) or earlier if wanted. |
| API keys with endpoints | Table only | Not in the Phase 2 brief; endpoints come with CI integration (Phase 11). |
| `failed_login_count`, `locked_until` columns on `users` | Lockout counters in Redis | They expire on their own and do not need a write to the user row on every failed attempt. |
| `audit_logs`, `agent_execution_logs` partitioned by month | Plain tables | Partitioning adds a maintenance job; deferred to Phase 13. |
| Email verification | Not implemented | No mail transport (open question Q5). |

### Known limitations

- **No email delivery.** Reset and invitation emails are written to the API log in development and dropped (with an error log line) in production. A mail transport must be chosen before anyone other than developers uses these flows.
- **Registration reveals whether an email is registered** (409 `email_taken`). Closing this needs email verification.
- **Account lockout can be triggered by anyone** who knows an email address, by failing five logins. The lock lasts 15 minutes and a password reset clears it.
- **Two tabs refreshing at the same moment** with the same refresh token will end the session, because the second use looks like token theft. The web app must refresh from a single place.
- **Composite tenant foreign keys cover the main chain only** (projects → applications → environments, and instructions, suites, cases, executions → projects). Other cross-references rely on row-level security plus service checks added with each feature.

### Decisions taken in Phase 2

| Topic | Decision |
|-------|----------|
| Access token | HS256 JWT with user and session ID only; no roles in the token. |
| Token storage | Access token in memory; refresh token in an HttpOnly cookie. Nothing in `localStorage`. |
| Outsider response | 404, not 403, for organizations the caller is not a member of. |
| Database roles | Separate owner and application roles; the application role is created by `python -m app.db.bootstrap`. |
| Client address | Taken from `X-Forwarded-For` only for proxies listed in `FORWARDED_ALLOW_IPS`. |

### Carried into Phase 3

- Project, application and environment endpoints on top of the existing models and `ProjectRepository`.
- Credential encryption service (the `credentials` table already has only ciphertext columns).
- URL and origin validation.
- OpenAPI-driven frontend types and the sign-in pages, whenever the frontend work is scheduled.

---

## Phase 3 — Project and Application Management

### Delivered

**Projects**

- Create, view, update, list, archive, restore and delete, with project members (`manager` / `member`).
- Delete safeguards: admin or owner only, the project must be archived first, its exact name must be typed, and no test may be running. Applications and environments are soft-deleted; their stored credentials are destroyed.
- An archived project is read-only all the way down.
- Access needs project membership, except for organization admins and owners. A project role never grants more than the organization role allows. Leaving the organization removes project access.

**Applications**

- Several per project, with name, description, base URL, type, framework, sign-in method, default browser configuration, allowed domains and status.
- Allowed domains are the outer boundary: every environment URL must fall inside them, and the list cannot be narrowed while an environment still depends on a host.

**Environments**

- Kinds `local`, `development`, `testing`, `staging`, `production`, `other`; base URL, API URL, extra allowed origins, variables (plain values or credential references), sign-in configuration, allowed browser actions, status.
- Production environments are admin-only, require https, and default to read-only with approval required.

**Application access validation** (`app/core/security/url_safety.py`)

- URL syntax checks, including look-alike and numeric-host tricks.
- Origin allowlisting per environment.
- DNS resolution with every resolved address checked; loopback, private, link-local and cloud-metadata addresses blocked. An admin-only, audit-logged switch permits loopback and private ranges for one environment; metadata and link-local stay blocked even then.
- An access check that follows redirects one hop at a time, re-validating each hop (including DNS), and connects to the address it validated so a DNS change in between cannot redirect it. Response bodies are never read.

**Test credentials**

- Seven kinds (username/password, basic auth, bearer token, API key header, cookie, TOTP secret, single value), stored with envelope encryption (AES-256-GCM, per-credential data key, versioned master key) and bound to their tenant and credential ID, or kept as a reference (`env:NAME`) so the value never enters the database.
- No endpoint returns a secret. Rotation, test-account confirmation, setup instructions, and a login recipe that must use placeholders.
- Per environment: a credential of one environment cannot be read, referenced or deleted through another.

**Frontend**

- Sign-in and registration (owed from Phase 2), session restore from the refresh cookie, automatic single-flight token refresh, organization switcher, sign-out.
- Project list and details, create/edit/archive/delete, member management; application list, registration and editing; environment management with access check; credential configuration with write-only secret fields.

### Validation results (2026-11-18)

| Check | Result |
|-------|--------|
| Backend tests | 348 passed: 183 without services, 165 against a scratch database, Redis and the worker |
| Ruff lint and format, mypy strict | Pass |
| Migration `0002 → 0003`, full downgrade to empty and upgrade again, model/schema drift check | Pass |
| Frontend ESLint, Prettier, `vue-tsc` | Pass |
| Frontend tests | 52 passed |
| TLS pinning against a real host | Correct host name connects; a wrong host name on the same address fails certificate verification; `localhost` is refused |
| Web app driven in a real browser (Chromium via Playwright) against the running stack | Passed with no console errors: redirect to sign-in, failed sign-in message, registration with a weak-password error, session restored after reload, create project, register application (bad URL rejected next to the field), add environment (unresolvable host rejected), store credential, access check against a real host, phone-width layout without horizontal scrolling, sign-out |
| Same run, security checks in the browser | Refresh cookie is `HttpOnly` and `SameSite=Strict` and not readable by scripts; nothing token-like in `localStorage` or `sessionStorage`; the stored secret is absent from the page after saving and the edit form shows an empty field |
| Screenshots reviewed | Desktop, 375 px phone width, dark theme. Two layout flaws found and fixed (credential name touching its tag; credential row buttons wrapping badly on a phone) |

What the tests cover, against the brief:

| Asked for | Covered by |
|-----------|-----------|
| Project permissions | Who can create, see, edit, archive, delete; manager vs member; viewer cannot be an effective manager; member management (`test_projects.py`) |
| Environment isolation | Separate allowlists and credentials per environment; cross-environment reads, references and deletes refused; an environment is reachable only through its own application (`test_applications.py`) |
| Application URL validation | 23 malformed or deceptive URLs; allowlist; private, loopback, metadata and mixed DNS answers; redirects leaving the allowlist; DNS rebinding between hops; connection pinning (`test_url_safety.py`, `test_applications.py`) |
| Credential security | Secret absent from every response, from the database in clear, from logs and from audit entries; encryption bound to tenant and credential; tamper detection; key rotation; no route can reveal a secret (`test_credentials.py`, `test_crypto.py`) |
| Tenant isolation | Another tenant gets 404 for every project, application, environment and credential URL, including mixed own-parent/foreign-child URLs; `project_members` under row-level security (`test_projects.py`, `test_tenant_isolation.py`) |

Two defects were found by these tests and fixed before completion: IPv6 loopback (`::1`) was refused even when private networks were permitted, and allowed-domain entries with surrounding spaces were rejected.

Not verified: the GitHub Actions workflow has still not run (no Git repository or remote yet).

### Deviations from the plan

| Plan | What was done | Why |
|------|---------------|-----|
| Paths like `/projects/{id}` | Everything nested under `/organizations/{org_id}/projects/…` | Row-level security needs the organization before any row can be read; nesting also makes cross-parent IDs unresolvable by construction. [api-design.md](api-design.md) §4 is rewritten; §5–12 note the same convention for later phases. |
| Allowlist only per environment | Plus an allowed-domain list on the application | The brief asks for allowed domains on the application; it now bounds every environment. |
| Only admins manage projects, applications, environments, credentials | Developers and QA engineers can create projects and manage applications, non-production environments and their credentials | The brief says developers and QA engineers register the applications they test. Production and network-widening settings remain admin-only. |
| Project members | New `project_members` table and `manager` / `member` roles | Asked for in the brief; not in the Phase 0 schema. |
| Environment `verify` through the egress proxy; credential `test-login` | An access check from the API process with the same validation and pinned connections; `test-login` not built | The runner and proxy arrive in Phase 6, the browser engine in Phase 7. |
| Frontend API types generated from OpenAPI | Written by hand | The generator does not yet support the TypeScript version in use. |
| `allow_private_network` permits all private targets | Loopback and private ranges only; link-local and metadata addresses always blocked | No legitimate test target lives there, and it is the address attackers aim for. |

### Known limitations

- **`allowed_domains` cannot tell a public suffix from a company domain.** An entry such as `co.uk` or `github.io` is accepted and would cover every site under it. Entries must have at least two labels, but a full public-suffix check is not included.
- **Carrier-grade NAT addresses (`100.64.0.0/10`) are always blocked**, including with private networks permitted. Applications reachable only over such a range (some VPN products use it) cannot be registered yet.
- **Environment hosts must resolve when the environment is saved.** A host that is temporarily absent from DNS cannot be saved until it resolves.
- **The access check runs from the API container**, so `localhost` there is the container, not your machine.
- **Secret references (`env:NAME`) are stored and validated but not yet resolved**; the runner that reads them arrives in Phase 6.
- **Allowed browser actions are stored and validated but not yet enforced**; enforcement belongs to the test engine and the MCP tool policy (Phases 6–7).
- **The secret-in-recipe check is a substring match** on values of six characters or more. A very short password would not be caught if typed into a recipe.
- **Still no email delivery** (carried over): invitations and password resets reach only people who can read the API log.

### Decisions taken in Phase 3

| Topic | Decision |
|-------|----------|
| Project access | Membership grants access; the organization role decides what may be changed. Admins and owners reach every project. |
| Outsider and non-member response | 404, as for organizations. |
| Credential deletion | Hard delete, including when a parent is soft-deleted. |
| Mixed DNS answers | One forbidden address among several is enough to refuse the host. |
| Frontend token storage | Access token in memory only; only the chosen organization is remembered in `localStorage`. |

### Carried into Phase 4

- Test instructions reference an application and environment; `ApplicationService.policy_for` gives the destination policy for a run.
- `CredentialService.reveal_for_execution` is the only way a stored secret leaves the database; it is ready for the test engine.
- Open: mail transport (affects invitations and resets), and whether to add a public-suffix check for allowed domains.

---

## Phase 4 — AI Requirement Analysis

### Delivered

**LLM integration** (`app/agent/llm/`)

- A provider-independent `LLMGateway` contract and an OpenAI implementation on LangChain's chat model.
- Model selection, timeouts, retry policy, output limit, temperature and token prices come from settings; there is no default model name. Business code never imports a provider SDK.
- Structured output: a strict JSON schema is sent, and the answer is validated again with Pydantic models that reject unknown fields. One repair attempt when validation fails; the rejected text is not fed back.
- Own retry loop with exponential backoff and jitter for timeouts, connection errors, 429 (honouring `Retry-After`) and 5xx. Rejected requests (400/401/403/404), refusals and truncated output are not retried.
- Provider errors are mapped to eight error codes whose messages contain no provider text, keys or prompt content.
- Every provider call, including failed attempts, is recorded with model, prompt version, tokens, cost (when prices are set), latency and outcome.

**Prompt management** (`app/agent/prompts/`): versioned prompt files loaded by name; the version is logged per call and stored with each analysis.

**Requirement analysis**

- The structured requirement covers: summary, application, module, feature, user actions, preconditions, test objectives by category, expected behaviour per objective with its source, business rules, validation requirements, negative scenarios, edge cases, required test data, dependencies, constraints, assumptions, missing information, clarification questions.
- Expected results are never invented: a result nobody stated has source `unknown` and an empty description, enforced by the schema, and becomes a blocking issue plus a question.
- Deterministic readiness checks: application active, environment active, feature identifiable, credentials present for the roles needed, expected behaviour stated for every objective, business-rule values known, test data available, read-only environments respected, and no target outside the application's allowed domains.

**Prompt-injection protection:** fixed system message, all variable text fenced as data with a per-request random ID, text normalisation, no secrets in the prompt context, a schema with no field for targets, permissions or approvals, scope checks on anything the text or the model names, and warnings to the reviewer.

**API:** submit, list, retrieve, change, re-analyse, answer clarifications, edit the analysis by hand, re-check readiness, approve as plan, retrieve plan, cancel. Analysis runs in the background worker through a `JobQueue` interface. Approval is for QA engineers and above; production needs an admin.

### Validation results (2026-11-18)

| Check | Result |
|-------|--------|
| Backend tests | 473 passed: 269 without services, 204 against a scratch database, Redis and the worker |
| Ruff lint and format, mypy strict | Pass |
| Migration `0003 → 0004`, full downgrade and upgrade, drift check | Pass |
| Gateway against simulated provider responses, through the real LangChain and OpenAI libraries | Success, cost calculation, strict-schema rules, repair, refusal, truncation, 429 with `Retry-After`, 5xx with backoff, timeouts, connection errors, rejected requests, missing configuration |
| Live wiring check with a stand-in OpenAI-compatible endpoint | API enqueued to Redis, the worker ran the job, the gateway retried after a 503 and succeeded over real HTTP, tokens and cost were recorded, approval produced plan version 1; the key did not appear in logs |
| Without a key | `503 llm_not_configured`, nothing stored |
| Frontend | Unchanged; 52 tests pass |

**Not verified: a real model.** No OpenAI API key was available, so nothing here has been run against the live API. Specifically unverified:

- that the provider accepts this schema in strict mode (a test checks the schema against the documented strict-mode rules, which is not the same thing);
- the quality of the analysis the prompt produces, including whether a real model follows the "never invent an expected result" rule;
- how well a real model resists prompt injection.

The first run with a real key should be treated as a test, starting with the two example instructions from the brief.

What the tests cover, against the brief:

| Asked for | Covered by |
|-----------|-----------|
| Structured output validation | Schema tests (invented results, dangling references, unknown and missing fields, size limits); gateway repair and rejection (`test_requirement_analysis.py`, `test_llm_gateway.py`) |
| Incomplete requirements | Missing credential, missing rule value, objective without a stated result, missing test data (`test_instructions.py`, `test_requirement_analysis.py`) |
| Ambiguous instructions | Unidentifiable feature; the loyalty example asking for the minimum amount, answered, then approved |
| Malicious instructions | Injection with out-of-scope URLs and the metadata address; a model that complies as far as the schema allows; fence spoofing; invisible characters; secrets absent from prompts; output with extra fields |
| LLM failures | Unavailable, invalid output, refusal, rate limit, rejected request, unexpected exception; retry afterwards |
| Timeout behaviour | Three timeouts through the real gateway end as `llm_timeout` with every attempt recorded |

One deployment defect was found by these tests and fixed: with `OPENAI_BASE_URL` passed as an empty string (which Docker Compose does for unset variables), the SDK used an empty endpoint. The gateway now always passes the endpoint explicitly.

### Deviations from the plan

| Plan | What was done | Why |
|------|---------------|-----|
| Frontend: instruction form, analysis view, clarification dialog | Not built | The Phase 4 brief is API-only. Owed in the frontend phase, or earlier if wanted. |
| Clarification and approval as LangGraph interrupts | Ordinary API calls and a background job | LangGraph arrives in Phase 8; the analysis step is written so the graph can call it unchanged. |
| "Approve testing plan" after plan generation | Approval of the analysed requirement, stored as plan version 1 | Plan and test-case generation are Phase 5; the brief places approval in this phase. Phase 5 extends the plan with strategy. |
| OpenAI key only in the worker | Also passed to the API container | The API needs to know whether AI features are configured. It never calls the provider. To be narrowed to a flag. |
| `langchain` and `langgraph` packages | Only `langchain-openai` (with `langchain-core`) added | Nothing uses the other two until Phase 8. |
| Roles | QA engineers can now approve; developers cannot | First permission that separates the two roles. |

### Known limitations

- **Source labels are the model's own claim.** The schema guarantees that an `unknown` result is empty, but cannot prove that a result labelled `stated_in_instruction` really is in the instruction. Human approval is the control; a grounding check could be added.
- **Injection phrase detection is a heuristic** and English-only. It produces a warning for the reviewer, never a verdict; the structural controls do not depend on it.
- **Bare host names in an instruction only warn.** A URL with a scheme, an IP address or `localhost` outside the application blocks approval; a bare name such as `shop.other.com` could be ordinary writing, so it warns.
- **One clarification answer can leave generated questions open.** When a rule is unknown, the checks ask about the rule and about each dependent objective. Answering the rule resolves them on re-analysis.
- **A stuck analysis is recoverable only by the user.** If the worker dies mid-analysis the instruction stays `analysing`; after five minutes it can be started again. Automatic reconciliation comes with Phase 8.
- **Instructions are single-shot polling.** No live progress events until Phase 8.
- **Budget enforcement needs prices.** Without configured token prices no cost is recorded and the monthly cap has nothing to compare.

### Decisions taken in Phase 4

| Topic | Decision |
|-------|----------|
| Model default | None. A key without a model name is a start-up error. |
| Size limits on model output | In validators, because strict mode does not accept length keywords in the schema. |
| Failed analysis | Stored as `status: failed` with an error code; never a partial result. |
| Readiness | Decided by code over the analysis and the platform's records; re-checked at approval. |
| Self-approval | Allowed for non-production: the author may approve if their role permits. |

### Carried into Phase 5

- An approved plan (`test_plans`, version N) with objectives, their stated expected behaviour and its source is the input for test-case generation.
- The gateway's `ModelRegistry` takes further purposes (`generation`, …) without code changes elsewhere.
- Open: a live run with a real key; whether the API container should stop receiving the key.

---

## Phase 5 — AI Test Case Generation

### Delivered

**Generation**

- Test cases are written from the approved plan by a background job, a few objectives per model call, under a strict schema. Categories: positive, negative, boundary, edge case, form validation, navigation, authentication, API validation, responsive UI, business logic.
- A case that verifies a plan objective takes that objective's expected result. Scenarios beyond the plan — empty, invalid, zero, negative or decimal input, missing related data, broken configuration — are generated **without** an expected result and cannot be approved until a person supplies one. No formula is assumed.
- Duplicates are removed within an answer and against the instruction's existing cases. Cases from before a mid-run failure are kept. Generated cases are collected into a draft suite.

**Structured format.** Every case has an ID (`TC-0001`, per project, never reused), title, description, category, preconditions, test data, execution steps, expected result with its source, priority, severity, tags, dependencies, automation feasibility and a verification strategy. Steps name elements in words rather than selectors, because the agent has not seen the application yet.

**Validation** (`app/testing/case_validation.py`), applied to AI-written and hand-written cases alike: meaningful steps, a verifying step, an expected result from a real source, defined and complete test data, no literal secrets, duplicates, relevance to the requirement, unsupported assumptions, addresses within the environment's allowlist, and the environment's permitted actions and read-only setting.

**Human review**

- Review, edit steps and expected results, delete, add cases by hand, regenerate selected cases with feedback, approve single cases or a whole suite.
- Every change is a new immutable version. Version 1 of a generated case stays exactly as the model wrote it; the approved version is recorded separately and stays on record when the case is edited later.

**Suites:** create, add and remove cases, group by feature, execution priority, execution order, approve. Approval is all or nothing and records the approved version of every case; changing membership or order makes the suite a draft again.

**Frontend:** instruction list and submission, requirement view with open questions and approval (owed from Phase 4), generated-case review with selection and regeneration, case page with editor and version history, suite list grouped by feature, suite page with ordering and approval.

### Validation results (2026-11-18)

| Check | Result |
|-------|--------|
| Backend tests | 547 passed: 321 without services, 226 against a scratch database, Redis and the worker |
| Ruff lint and format, mypy strict | Pass |
| Migration `0004 → 0005`, full downgrade and upgrade, drift check | Pass |
| Frontend ESLint, Prettier, `vue-tsc` | Pass |
| Frontend tests | 61 passed |
| Web app driven in a real browser against the running stack, with a stand-in for the model | Passed with no console errors: submit instruction, question shown instead of a guess, answer, approve requirement, generate 10 cases, open a case without an expected result (approval disabled), edit it, see approval still blocked by a placeholder step, fix it, approve, view the AI's original version, regenerate a selected case, suite approval refused with the blocking cases listed, remove and reorder cases, approve the suite, phone-width layout |

**Not verified: a real model.** As in Phase 4, no OpenAI API key was available. The generation prompt has never been answered by a real model, so the quality of generated cases, how well a model keeps to the no-guessing rule, and whether the provider accepts the larger schema in strict mode are untested. The tests and the browser run use answers written to match what the prompt asks for.

What the tests cover, against the brief:

| Asked for | Covered by |
|-----------|-----------|
| Generation | End-to-end through the worker job; batching by objective; prompt contents; partial and total failure; no key (`test_test_cases.py`) |
| Schema validation | Case schema (required parts, unknown fields, enumerations, limits); strict-mode rules for the schema sent to the provider; a guessed result rejected and repaired through the real gateway |
| Duplicate detection | Same scenario despite different wording; duplicates removed at generation; duplicate of an existing case flagged |
| Boundary scenarios | The loyalty example: 499 / 500 / 501 keep their values and plan results; seven further scenarios generated with no result |
| Missing expected results | Blocked from approval individually and in a suite; database constraint; a person supplying the result |
| Manual editing | New version per edit; AI original unchanged; edits under the same schema and checks; approved version kept; manual cases; deletion |

One gap was found by looking at the browser run and fixed: a case could be approved while its checking step still held the generator's `TO BE DEFINED` placeholder. That now blocks approval.

### Deviations from the plan

| Plan | What was done | Why |
|------|---------------|-----|
| Steps with locators (role, label, test ID) in a shared `testdsl` package | Steps with a fixed action list and elements described in words, in `app/testing/cases.py` | The agent cannot see the application until Phase 6; an invented selector would only look precise. Locators are attached to these steps when exploration exists, and the model moves to a shared package when the runner needs it. |
| Plan generation as a separate step with strategy, viewports and data strategy | Cases are generated directly from the approved requirement | The brief for Phases 4 and 5 does not ask for a separate plan document; the approved requirement serves as the plan. |
| Regeneration loop driven by validator errors (up to two rounds) | Invalid cases are kept, flagged and blocked; a person edits or regenerates them | Keeps people in control of what is rewritten and avoids unattended model loops. |
| `test_suites.status` of `active` / `archived` | `draft` / `approved` / `archived` | Suites need an approval state. |

### Known limitations

- **Generated steps are not executable yet.** They describe elements in words. Turning them into runnable steps needs the browser integration (Phase 6) to find the elements.
- **`configured_rule` is trusted from the model.** The checks cannot verify that a result labelled as following from a configured rule really does; human approval is the control.
- **Duplicate detection is exact after normalisation**: same title, or same steps and data. Two differently worded cases that test the same thing in different ways are not recognised.
- **Regeneration relies on the model returning the cases in the order asked.** A model that returns fewer leaves the remaining cases unchanged.
- **Relevance is structural.** A case is relevant if it is linked to a plan objective; whether its steps really exercise that objective is for the reviewer.
- **The secret-in-step check is a heuristic** on password-like field names, and is relaxed for negative and validation cases, where deliberately wrong input is the point.
- **Editing in the web app is plain forms.** Lists are one item per line, test data is `name=value`; there is no drag-and-drop.
- **No pagination** on case and suite lists yet (limit 500 cases per query).

### Decisions taken in Phase 5

| Topic | Decision |
|-------|----------|
| Scenarios beyond the plan | Generated, flagged as beyond the requirement, with no expected result. |
| Who labels a result | The server. A result a person writes or changes is `human_specified`, whatever the request says. |
| Suite approval | All or nothing; pins the approved version of each case. |
| Editing after approval | The case needs review again; the suite keeps its pinned version and shows the member as outdated. |
| Invalid generated cases | Kept and blocked, not silently dropped, so reviewers see what the model produced. |

### Carried into Phase 6

- Approved suites with pinned case versions are what will be executed.
- Each step has `action`, a described `target`, `value` and `side_effect`; exploration has to resolve targets to locators and store them on the step.
- Open, as before: a live run with a real key.

## Phase 6 — Playwright MCP Integration

Full description: [playwright-mcp-integration.md](playwright-mcp-integration.md).

### Delivered

**MCP client** (`app/mcp/`), on the official MCP Python SDK over streamable HTTP: connection management, handshake and capabilities, tool discovery with pagination, argument validation against the discovered schemas, invocation, result parsing, ping health check, per-call timeouts, idempotent termination, typed errors, and reconnection where it is safe. It contains no tool names.

**Browser tools and permissions** (`app/browser/policy.py`). Default deny. Each tool of the pinned server is classified as read-only, reversible UI, state-changing UI or sensitive, and tied to an environment action. A session gets a tool only if the server discovered it, a rule exists and matches the server's description, the environment allows it, the workflow asked for it, and an admin has not switched it off. Arguments not named by the rule are refused. Running code in the page, running Playwright code, and file upload cannot be enabled.

**Sessions** (`app/browser/session.py`, `manager.py`, `runner/`). One Playwright MCP process and browser per session, in-memory profile, own directory, own proxy credentials, configurable browser, viewport and timeouts, bounded lifetime, guaranteed cleanup on success, error and cancellation.

**Staying in scope** (`app/egress/`). An egress proxy on an internal network is the browser's only way out and connects only to the session's allowed origins, after resolving and checking the address itself. Sessions also check addresses before and after each call. Downloads are refused.

**Authentication** (`app/browser/login.py`). Login from the credential's recipe, without any model. Secrets are decrypted in the worker (audited), typed at call time, and redacted from arguments, results, errors, logs and database rows.

**Observability.** Every tool call, including refused ones, is recorded with session, tool, category, sanitized arguments, status, duration and error; connection status changes are recorded; the organization's connection row keeps server version, discovered tools and health.

**Failure handling.** Runner or proxy unavailable, server unavailable, discovery failure, invalid arguments, launch failure, timeouts, navigation failure, unexpected browser closure and lost connection each map to a typed error; none can produce a pass.

**API.** Browser status and tool permissions; admin restriction of tools; browser checks on an environment (open the page, optionally sign in) run by the worker.

**Agent adapter** (`app/agent/tools/browser.py`). Turns a session's permitted tools into LangChain tools with fenced output, ready for the LangGraph agent. Nothing calls it yet.

### Found by checking the real server first

The Phase 0 design listed the server's tools and options as unverified. Probing version 0.0.86 before writing code changed the design in these ways:

| Finding | Effect |
|---------|--------|
| Results echo generated code, including typed passwords | `--codegen none` on the server, and value redaction in the client |
| A crashed browser is relaunched silently | The session detects the empty page and reports the browser as closed |
| Navigating to a download saves the file automatically | Downloads disabled in the browser; a download event ends the session |
| The server's own hints mark nearly every tool as destructive | Classification is a reviewed table, cross-checked against the hints |
| Pages can register tools through WebMCP | Disabled; unknown tools are withheld anyway |
| Elements can be addressed by role and name directly | Login recipes need no snapshot parsing |
| SDK 2.x differs from the 1.x API and reports transport failures by cancelling the caller | One owner task per connection; verified with a mock and a real server |

Two defects were found by the tests against the real browser and fixed:

1. When the proxy refused a plain-HTTP page, it answered with an error page. The browser showed that page at the refused address, so the navigation looked successful — a possible false pass. The proxy now closes such connections without an answer and the navigation fails.
2. The proxy refused the runner's address but could reach its own admin port through its second network interface. It now refuses every address of its own machine.

### Validation performed

| Check | Result |
|-------|--------|
| Ruff, format, mypy strict (157 files) | Pass |
| Unit tests | 434 pass (113 new) |
| Unit + integration with PostgreSQL and Redis | 682 pass (21 new for browser checks, 1 for recipes) |
| Real server, browser, proxy and demo app (`make be-test-browser`) | 11 pass, run three times |
| Runner supervisor (`make runner-test`) | 11 pass |
| Live run through the running stack (API over HTTP, arq worker, runner, proxy) | Page check and login check both `passed`; password absent from the response and the worker log; no processes or files left in the runner |
| Isolation, tested from inside the containers | Runner cannot reach PostgreSQL, Redis, the API, the internet or the metadata address; read-only file system; no platform secrets in runner or proxy; the API has neither service token |

Test coverage asked for in the brief:

| Area | Where |
|------|-------|
| Tool discovery | `test_mcp_client.py` (schemas, hints, pagination, failure, unusable schemas); real contract test |
| Tool invocation | `test_mcp_client.py`, `test_browser_session.py`; real tour of navigate, snapshot, find, click, type, select, dialog, screenshot, console, tabs |
| Invalid arguments | Schema violations, forbidden arguments, no values in messages; nothing sent to the server |
| Connection failure | Unreachable server, hung handshake, server lost mid-call, lost before and after the first action |
| Timeout | Client timeout with the connection still usable; server-reported timeout; real slow page |
| Session cleanup | Order of set-up and tear-down, cleanup on error and cancellation, no leftover tasks, runner process groups, real runner session count |

### Deviations from the plan

| Plan | What was done | Why |
|------|---------------|-----|
| `langchain-mcp-adapters` | The official `mcp` SDK directly, plus a small adapter to LangChain tools | The brief asks for the official SDK; it also gives control over errors, timeouts and validation. |
| Runner reverse-proxies MCP with signed per-session tokens | The worker connects to the session's port on the internal network; runner API protected by a shared token | A streaming reverse proxy is considerable code for little gain while only the worker, runner and proxy share that network. Listed as a limitation. |
| Allowlists pushed to the proxy by the runner | Pushed by the worker | The runner is the least trusted component; it should not define its own limits. |
| Chromium sandbox enabled with a seccomp profile | `--no-sandbox`, as in the official image | Needs host-specific work; open for Phase 13. |
| Login executed by the test engine, state passed to MCP as a storage-state file | Login executed through MCP tools; no state stored | The engine is Phase 7. Not storing cookies also avoids protecting them at rest. |
| Exploration wired into plan generation; element inventory | Not built | The brief for this phase covers the integration, not exploration, and says to avoid unnecessary autonomous loops. The adapter is ready for Phase 8. |
| MCP settings page | Not built | The brief for this phase lists no frontend work. The API is there. |
| CRUD for several MCP connections, external servers | One managed connection per organization, created on first use | Nothing uses more yet. |
| New `browser_checks` table | State in Redis for one hour; trace in `agent_execution_logs` | A check is a diagnostic, not a record; no migration was needed. |
| Free-form login recipe (Phase 3) | A defined, validated recipe format | It has to be executable. Recipes saved in the old free form are rejected when used and must be re-entered. |

### Known limitations

Listed in full in [playwright-mcp-integration.md](playwright-mcp-integration.md) §13–14. The main ones:

- No model has driven a browser yet; the agent adapter is tested only with a mock.
- Chromium runs without its own sandbox.
- Session MCP ports rely on network isolation, not on authentication.
- Result parsing depends on an undocumented layout of the pinned version.
- Chromium only. No TOTP, CAPTCHA, SSO or reference credentials for login.
- Screenshots are returned but not stored; there is no evidence store yet.
- The GitHub Actions workflow, including the new browser job, has still never run.

### Decisions taken in Phase 6

| Topic | Decision |
|-------|----------|
| Unknown tools | Never exposed; a contract test fails when the server offers one that is not classified. |
| Reconnection | Only before a session's first action. Later, a lost connection fails the session. |
| Leaving the allowed origins | Ends the session; it is not resumed. |
| Timeouts | Reported as failures; never retried automatically for state-changing tools. |
| Login on approval-required environments | Only an organization admin can start it; that request is the approval and is audited. |
| Who may start a browser check | Developers and above (`browser:run`); viewers cannot. |

### Carried into Phase 7

- The test engine joins the runner image and should reuse the session's proxy credentials and allowed origins.
- Evidence storage, so screenshots and traces can be kept.
- Verdicts for test runs must come from the engine's assertions, with `BrowserError` meaning "not performed".
- Open, as before: a live run of the AI features with a real key.

## Phase 7 — Playwright Test Execution Engine

Full description: [test-execution-engine.md](test-execution-engine.md).

### Delivered

**Architecture.** A separate engine service on the official Playwright image, one process per run, one browser context per case attempt. Separate modules for the test case representation, step execution, browser management, locators, actions, assertions, evidence and result persistence (table in the engine document, §1). No model is called anywhere in running or judging a test.

**Controlled step system** (`app/testing/automation.py`). Thirty-one step types, each with a strict schema: navigate, click, fill, clear, select option, check, uncheck, select radio, upload file, wait for element, wait for navigation, read text, read attribute, screenshot, API request, and sixteen assertions. No step runs code, sleeps, or takes a raw selector.

**Locators.** Role, label, text, placeholder, test ID and CSS, with optional scoping to a parent. A locator must match exactly one element; none or several is an error, not a guess. XPath and selector-engine tricks are rejected by the schema; CSS is flagged as fragile.

**Assertions.** Text, visibility, existence and count, enabled and disabled, checked, input value, address, title, browser validation message, API status, API response data by JSON Pointer, and business rules through a safe expression evaluator with decimal arithmetic. Assertions wait for the page; there are no fixed sleeps. "Does not hold" (failed) and "could not be evaluated" (error) are kept apart.

**Test data.** Project data sets with environment-specific overrides, the case's own data, environment variables, generated unique values, approved upload files, cleanup steps that always run, and checks that every value a script uses exists. Data must be declared synthetic; real-looking card numbers are refused.

**Configuration.** Browser, viewport, action, navigation and case timeouts, retry policy, screenshot, video and trace settings.

**Isolation.** Engine container on the internal network behind the egress proxy, with CPU, memory and process limits, time limits at four levels, read-only file system, and in-browser blocking of other origins and of writes on read-only environments.

**Result storage.** Execution, results, every step of every attempt (times, action result, assertion expected and actual, error details, locator used), evidence files with references to result and step, final status and verdict.

**API.** Scripts on cases, data sets, test files, executions (start, list, get, cancel), steps, evidence list and download.

### Found by running it for real

| Finding | Fix |
|---------|-----|
| Finished browser processes stayed in the engine container as zombies; over time this would have exhausted the process limit and stopped all runs | An init process in the engine container; verified zero zombies after runs |
| When the proxy refused an API step, the engine saw the proxy's `403` as the application's answer. A test asserting 403 would have passed without the application being reached | The engine recognises the proxy's response and reports `destination_not_allowed` |
| The engine's evidence endpoint would also have served a run's diagnostic log | Only evidence file types are served |
| Comparing a JSON `true` with an expected `true` failed | JSON equality rewritten, with tests for numbers, booleans and nested values |
| A step's duration included the time to take its failure screenshot (up to several seconds) | The step ends before evidence is taken |

### Validation performed

| Check | Result |
|-------|--------|
| Ruff, format, mypy strict (194 files) | Pass |
| Unit tests | 601 pass (167 new) |
| Unit + integration with PostgreSQL and Redis | 882 pass (33 new for executions, scripts and test data); migration round trip included |
| Engine in real browsers (`make engine-test`) | 79 pass, run three times; includes Firefox and WebKit |
| End to end with nothing faked (`make be-test-browser`) | 17 pass (6 new) |
| Runner supervisor | 11 pass |
| Frontend | 61 pass, unchanged |
| Live run through the running stack (API over HTTP, arq worker, engine, proxy) | Good build passed; seeded defect failed with the correct expected and actual values; screenshot, trace and video downloaded through the API; no leftover processes or files in the engine |
| Engine isolation, tested from inside the container | No route to PostgreSQL, Redis, the API, the internet or the metadata address; file system and code read-only; no platform secrets; the API has no engine token and mounts evidence read-only |

Test coverage asked for in the brief:

| Area | Where |
|------|-------|
| Each supported action | `tests/engine/test_engine.py`: every action passing, and failing in its typical ways; schema for all 31 in `test_automation.py` |
| Locators | Every strategy finds its element; ambiguous and missing are errors; XPath and tricks rejected |
| Assertions | Every assertion holding and not holding; text comparison modes; waiting for late content |
| Timeouts | Step, navigation and case timeouts; execution timeout; engine run budget |
| Cleanup | Runs after failure; removes created data; a failing cleanup is flagged and does not change the verdict; processes and directories removed; cancellation stops the run |
| Failure conditions | Off-scope addresses, read-only and action limits, failed sign-in, engine unavailable, engine aborting mid-run, missing evidence, blocked scripts |
| Runs without an LLM | The whole of `make engine-test`; the end-to-end suite asserts the model stand-in received no request |

### Deviations from the plan

| Plan | What was done | Why |
|------|---------------|-----|
| Engine inside the runner container | A separate `engine` service and image | The Playwright Python package only drives the browser builds it was released with; the MCP image carries different ones. Two pinned images are simpler to upgrade than one mixed image. |
| Shared `testdsl` package | The schema lives in `app/testing/`; the engine image contains the backend package | One definition without a second package to version. The engine process is given none of the backend's settings. |
| Steps attached to `test_steps` rows | One script per case version in `test_case_versions.automation` | One descriptive step often needs several executable ones. `test_execution_steps.test_step_id` stays empty. |
| Engine shares an evidence volume | The worker fetches evidence from the engine and stores it | The engine then has no access to stored evidence of any organization. |
| Signed evidence URLs | Authenticated download through the API | No web page consumes them yet; fewer moving parts. |
| Locator fallbacks; self-healing | Not built | Not in the brief for this phase; strictness was. |
| Viewport matrix, data-driven rows | One viewport and one data row per execution | Not in the brief for this phase. |
| Audit crawl, findings | Not built | Not in the brief for this phase. |
| Frontend: run action, execution pages | Not built | The brief for this phase lists no frontend work. |
| `verify` and `test-login` endpoints | Covered by the browser check of Phase 6 | Same purpose. |
| Login state handed over as a storage-state file | Session state held in memory, per case | Nothing with cookies is written to disk. |

### Known limitations

Listed in full in [test-execution-engine.md](test-execution-engine.md) §13–14. The main ones:

- Scripts have to be written by hand through the API; nothing generates them yet, and there are no web pages for any of this phase.
- Chromium runs without its own sandbox in the engine container.
- Evidence shows whatever the application displays; only credential secrets are kept out.
- Expired evidence is not deleted yet.
- One viewport and one data row per execution; no reports; no rerun.
- The GitHub Actions workflow has still never run.

### Decisions taken in Phase 7

| Topic | Decision |
|-------|----------|
| Failed versus error | Kept apart at every level. An error is never evidence about the application. |
| A case with no assertion | Cannot be approved; if one ran anyway it is an error, not a pass. |
| Passing on retry | `flaky`, never `passed`. |
| Cases that did not run | Make the execution `inconclusive`, never `passed`. |
| Cleanup | Always attempted; its failure is flagged and does not change the verdict. |
| Retries | Off by default; when on, only for errors unless asked otherwise. |
| A case that types a secret itself | No video or trace for that case; screenshots with the field painted over. |
| Who may add upload files | QA engineers and admins; adding is the approval. |
| Approval-required environments | Only an admin can start a run that changes data or signs in; the worker enforces it independently. |

### Carried into Phase 8

- The agent has to produce `Script` objects; everything it produces goes through the same schema, pre-run checks and human approval as hand-written ones.
- `ExecutionRunner` is a linear job; the graph replaces the orchestration, not the engine.
- Open, as before: a live run of the AI features with a real key.

## Phase 8 — LangGraph AI Testing Workflow

Full description: [agent-workflow.md](agent-workflow.md).

### Delivered

**The graph** (`backend/app/workflow/`). A LangGraph `StateGraph` with the fifteen required nodes and a wait for clarification. Nodes call the existing services (analysis, generation, executions) as the person responsible, whose access is checked again at every node.

**State.** A typed state with every field the brief lists, holding identifiers and summaries. No secrets, page content or scripts; tests read the stored checkpoints to confirm it.

**Conditional edges**, as pure functions: requirement complete or incomplete, test cases valid or invalid, approval required or not, execution and tests passed or failed, retry allowed or not, report generated or not. Any failed, rejected or cancelled run still reaches the report.

**Tool selection** by fixed policy: Playwright MCP with read-only tools for looking at the application, the deterministic engine for approved cases, the engine's API steps for backend checks. No tool is given to a model.

**New model calls**, both with strict schemas, versioned prompts and fenced input: writing the executable script of a reviewed case, and explaining failures (advice only).

**Human in the loop.** The run waits for answers to clarification questions and for approval, naming why approval is needed: generated cases, state-changing operations, destructive cleanup, cases outside the requirement. It resumes from its checkpoint when a person has decided.

**Retries.** Only transient failures, only if nothing may have changed data, bounded, every attempt recorded as its own execution. A pass on retry is flaky.

**Persistence.** Checkpoints after every node in PostgreSQL through a checkpointer written on the application's database layer (tenant-isolated, no second driver); the run, its node history, attempts, approval and errors in tables.

**Recovery.** Runs whose worker stopped are found and continued automatically, or on request. An execution that was running is closed, not run again; what happens to its cases follows the retry rules.

**Observability.** Node durations and outcomes, model calls and tokens, MCP calls, browser actions, retries, interrupts and errors, per run, through the API and in the report.

**API.** Start, list, get, answer questions, approve or reject, cancel, recover, report.

### Found by running it

| Finding | Fix |
|---------|-----|
| After a retry was prepared, the edge checked the wrong condition and the retry was never executed | The edge now asks whether the current execution has been recorded |
| A requirement that stayed incomplete with no question left to ask would have waited for ever | The run fails with `requirement_incomplete` |
| An execution prepared just before a cancellation stayed open and would have counted against the organization's limit indefinitely | Closing a run closes any execution it left unfinished |
| Killing the real worker left the run "running" with nothing to notice it | Workers now list the runs they work on and recover those whose lock has expired |
| The engine image had not been rebuilt after a dependency was added (only the backend image was), so the engine's tests could not start | Rebuilt. The engine image installs the backend's dependencies, so `make build` after any dependency change is needed for it too |

### Validation performed

| Check | Result |
|-------|--------|
| Ruff, format, mypy strict (222 files) | Pass |
| Unit tests | 648 pass (47 new) |
| Unit + integration with PostgreSQL and Redis | 968 pass (39 new for the workflow); migration round trip included |
| End to end with real browsers (`make be-test-browser`) | 19 pass (2 new): real MCP discovery, real engine, demo application |
| Engine suite, runner suite, frontend | 79, 11 and 61 pass, unchanged |
| Live run through the running stack with the real queue worker and the real gateway code against a stand-in model endpoint | Instruction to report: approval pause, 3 model calls, 12 MCP calls, both cases passed |
| Worker container killed in the middle of an execution | Run continued by itself from its checkpoint; the case in progress reported as an error and not repeated; the case not yet started run and marked flaky |

Test coverage asked for in the brief:

| Area | Tests |
|------|-------|
| Successful workflows | Instruction to report with approval; approved suite without approval; real end-to-end |
| Incomplete requirements | Questions, answers, continuation; the limit of rounds; nothing left to ask; a failed analysis |
| Rejected approvals | Nothing executed, instruction and plan rejected; who may decide; admin-only cases |
| Browser failures | Application unreachable; browser service unavailable; engine unavailable and retried |
| Assertion failures | Reported with expected and actual, explained as advice, never retried; a model cannot turn one into a pass |
| Retries | Transient error retried within the limit; limit reached; not after data may have changed; not for permanent errors |
| Interrupted workflow recovery | Between nodes; during an execution, with and without state-changing steps; automatic recovery; repeated interruptions |

### Deviations from the plan

| Plan | What was done | Why |
|------|---------------|-----|
| `AsyncPostgresSaver` from `langgraph-checkpoint-postgres` | Own checkpointer on SQLAlchemy and asyncpg | The package needs psycopg as a second driver, and its tables would be outside row-level security. |
| A model chooses tool calls in bounded exploration and failure investigation loops | Exploration is a bounded, code-driven read-only visit; failure analysis works from recorded facts | The standing instruction is to avoid unnecessary autonomous loops and never expose unrestricted tools. Code-driven discovery gives the model what it needs to write scripts and leaves nothing for page content to steer. |
| Three modes including `generate_only` | `generate_and_run` and `run_existing` | Generation without running already exists as the Phase 4 and 5 endpoints. |
| Plan approved by a person before generation | Plan and cases approved together at one checkpoint | The brief's checkpoints are about cases and operations; one decision with everything in view. |
| Self-healing of locators | Not built | Not in the brief for this phase. |
| Live events over SSE; frontend pages | Not built; the run is polled | The brief lists no frontend or streaming work. |
| Budgets per run | The existing monthly cap and per-instruction limits apply | Not in the brief. |
| arq retries crashed jobs | A scheduled recovery job driven by Redis locks | arq re-runs a killed job only after its long timeout; recovery within a minute needed its own mechanism. |
| Report with a narrative written by a model | A report built entirely from stored facts | Fewer unverifiable statements; the failure explanations are the only model text in it, marked as advice. |

### Known limitations

Listed in [agent-workflow.md](agent-workflow.md) §15–16. The main ones:

- **No real model has run this workflow.** Four prompts now exist that have only been exercised against stand-ins and the strict-schema rules.
- Scripts are grounded in an outline taken without signing in; pages behind the login are written from the case's wording.
- No web pages: runs, approvals and reports are API only.
- Checkpoints are never pruned.
- The GitHub Actions workflow has still never run.

### Decisions taken in Phase 8

| Topic | Decision |
|-------|----------|
| Who a node acts as | The person who started the run, or the person who decided the approval, with access checked at every node. |
| What a model may do | Write content against a strict schema, at four fixed points. Nothing else. |
| Approval | Always for generated cases. Admin for state changes on approval-required environments and for production. |
| Cases that are not ready at approval | Left out and reported; the ready ones run. |
| Retry | Transient failures only, never if data may have changed, at most twice. |
| An execution interrupted by a worker stopping | Closed, never re-run as a whole. |
| A pass on a retry | Flaky. |
| Runs that end early | Always produce a report. |

### Carried into Phase 9

- The failure explanations and the `locator` failure class are the starting point for self-healing proposals.
- A live run with a real model is the main open risk across Phases 4, 5 and 8.

## Phase 9 — Test Validation, Failure Analysis and Self-Healing

Design: [result-validation-and-healing.md](result-validation-and-healing.md).

### Delivered

**Classification** (`app/testing/classification.py`)
- Every result that did not pass gets one of seven categories by rule: application failure, test assertion failure, test automation failure, browser infrastructure failure, network failure, authentication failure, inconclusive. Stored in `test_results.failure_category` with the facts it rests on.
- New status and verdict `needs_review`, between `inconclusive` and `flaky` in precedence. Statuses now cover passed, failed, skipped, blocked, error, flaky and needs review.

**Expected-result verification**
- Each result stores what its verdict rests on: assertions evaluated and held, by kind (UI, API, business rule).
- In a workflow, a pass that the stored steps do not support becomes `needs_review` (was `inconclusive`).

**Failure analysis** (`app/services/failure_analysis.py`, prompt `failure_analysis.v2`)
- One failure record used everywhere: case, failed step, expected, actual, browser error, console and network errors, screenshot evidence, execution log, category.
- The model's answer now has a summary, possible root cause, impact, debugging steps and suggested further test cases. Stored with a fixed label ("AI-generated suggestion. Not a confirmed root cause…"), the rules' category, and whether the two agree.
- Available in workflow reports and on request for any execution.

**Self-healing** (`app/engine/healing.py`)
- The eight-step recovery process, by rules only, for locators that match nothing. Modes `off` (default), `propose`, `recover`; a limit per case.
- A recovered case is `needs_review`, not `passed`. Both locators are stored on the step and in `locator_heal_proposals`.
- Proposals: list, accept (a new case version with that one locator replaced, which needs approval), reject. Audited.

**Flaky detection** (`app/testing/flakiness.py`)
- Per case, from result history within the same version and environment: pass on retry, outcome flipping, healed runs. Kinds: timing, unstable locator, network, environment, intermittent. Read-only; nothing is hidden or re-labelled.

**Other**
- Migration `0008`; API section `quality` (7 endpoints); demo application page `/healing`.

### Found by running it

| Found | What was done |
|-------|---------------|
| With Playwright's default (non-exact) name matching, a `"Subscribe"` locator already matches an "Unsubscribe" button if it is the only one — before any healing | Not changed (it is Playwright's behaviour and scripts rely on loose matching); documented, with the advice to use `exact: true` for sensitive actions |
| The page title was a second heading containing the same word, so the engine refused to choose between two equally close headings | Correct behaviour; the test page was changed, and the case kept as a test of ambiguity |
| `assert_visible` on a missing element is a failed assertion, not a missing locator, so it is never healed | Kept, and tested as such: an assertion that something is visible must not go looking for something else |
| Two replacements found in one run could not both be accepted, because accepting the first created a new case version | Acceptance now checks the step itself (same position, same action, same old locator) instead of the version |
| Results of one stand-in run all finish at the same instant, which made the order of history arbitrary | History is ordered by finish time, then creation time |

### Validation performed

| Check | Result |
|-------|--------|
| Ruff, format, mypy strict (232 files) | Pass |
| Unit tests | 713 pass (65 new) |
| Unit + integration with PostgreSQL and Redis | 1047 pass (79 new); migration round trip included |
| Engine suite in a real browser | 98 pass (19 new, all on locator recovery) |
| End to end with nothing faked (`make be-test-browser`) | 21 pass (2 new): error → recovered → accepted → approved → passes as written; real failures land in the right category |
| Runner suite, frontend | 11 and 61 pass, unchanged |
| Running stack | Rebuilt and healthy; the new routes answer. No separate manual run through the queue was made this phase: the end-to-end suite drives the same API and worker code against the real engine |

Test coverage asked for in the brief:

| Area | Tests |
|------|-------|
| Classification | A table of 25 combinations; stored categories through the API; real failures in a real browser; cases that never ran |
| Root-cause suggestions | Record contents; what the model is and is not shown; labelling; a model that claims a pass or blames the application for a browser crash; permissions; no model configured |
| Locator recovery | Renamed field and button recovered with both locators recorded; `within` respected; propose mode; acceptance and approval; rejection; stale proposals |
| Retry boundaries | One attempt per step; limit per case; cleanup and sign-in not healed; workflow does not retry a healed or failed case |
| False positives | Opposite names, destructive names, two equally close candidates, wrong role, no name, CSS and test-ID locators, failed assertions, expected values never changed — each checks that nothing was clicked |
| Flaky detection | Flipping outcomes, pass on retry, kinds; regressions, fixes, version or environment changes and short histories are not flaky; a flaky result is never counted as passed |

### Deviations from the plan

| Plan | Built | Why |
|------|-------|-----|
| Bounded MCP investigation of a failure | Not built | The engine already records the page state it needs at the moment of failure; a second browser session per failure would add cost and a loop for little gain |
| Proposals verified by a full deterministic re-run | Verified inside the run: the same step is carried out with the replacement, and the rest of the case, with its assertions, runs after it | A separate re-run would repeat steps that may change data |
| Web pages for results, proposals and flaky indicators | Not built | As in Phases 6–8, API only |
| Evaluation harness started | Not started | Needs a live model |
| `blocked` for remaining cases when the environment is down | They are `skipped` or `error` with category `browser_infrastructure_failure` or `inconclusive` | Existing statuses already say this; the category now says why |

### Known limitations

- Failure analysis has only run against a scripted stand-in, like every other prompt.
- Recovery compares element names with English word lists. It cannot know what a button does; hence review, acceptance and approval.
- Only missing elements are healed, and only when the original locator names the element.
- Healing is off unless an execution asks for it.
- Flaky detection needs three runs of the same case version in the same environment.
- Analysis on request makes its model call inside the API request.
- The same person can accept a replacement and approve the resulting version, as with any edit.

### Decisions taken in Phase 9

| Decision | Reason |
|----------|--------|
| A recovered run is `needs_review`, never `passed` or `flaky` | Every step passed, but not as the approved script was written |
| Healing changes the locator only | Changing an expected value to make a test pass is exactly what must not happen |
| No model in classification, healing or flaky detection | They decide what a result means; a model only explains |
| The model is told the rules' category and its disagreement is shown, not resolved | A reader sees both; the rules stand |
| Accepting a proposal creates an unapproved version | A replacement locator is a script change like any other |
| Flaky results are reported, not quarantined | Hiding them is how flaky tests turn into false passes |

### Carried into Phase 10

- `failure_category`, the failure record and stability data are ready for reports and dashboards.
- Web pages for Phases 6–9 are still missing.
- A live run with a real model remains the main open risk.


## Phase 10 — Test Reports and Evidence Management

Design: [reports-and-evidence.md](reports-and-evidence.md).

### Delivered

**Evidence**
- Console and network logs as evidence files, next to screenshots, video and traces. A `logs` capture setting beside the existing ones: off, on failure (default) or always.
- A storage interface (`EvidenceStorage`) with the directory store as its one implementation.
- Checks before a file is stored: known kind, matching content type, content signature, size per kind, quota per execution. A refused file is noted on the result and changes no status.
- Retention by kind (30 days; 14 for video and trace), refusal of expired files at once, and an hourly worker job that deletes them.
- Download addresses for one file: signed, bound to the user and sign-in session, valid for five minutes, every access check repeated when used, audited.
- Log files are redacted a second time when written.

**Reports** (`app/reporting/`)
- One report model built from stored rows, rendered as a web page, JSON, CSV and PDF.
- Contents as listed in the brief, including failure summaries, step details, screenshots and AI suggestions labelled as such.
- Executions started by the agent workflow now record their instruction, so the report can name it.

**History and dashboard**
- Execution history with filters and paging, test case history, failed test history, comparison of two executions, daily trends.
- Dashboard: total executions, pass and failure rate, flaky test count, average duration, frequently failing cases, recent executions, test coverage indicators.

**Web application**
- Five pages: dashboard, execution history, test report, comparison, result history of a case. Linked from the project page.

**Other**
- New dependency `fpdf2` (PDF). No database migration.

### Found by running it

| Found | What was done |
|-------|---------------|
| A case without a script stores JSON `null`, not SQL NULL, so "has a script" counted every approved case as automated | The query checks that the value is a JSON object; covered by a test |
| The stand-in engine stamps results with a fixed past time, which showed that "last N days" was filtering on when a result finished | Periods now go by when the execution was started, the same everywhere |
| The dark theme's accent colour was too light for a chart mark | A separate chart colour, checked against both surfaces |
| A day without results read "0 of 0 cases passed" under the chart | It now says nothing ran that day |

### Validation performed

| Check | Result |
|-------|--------|
| Ruff, format, mypy strict (244 files) | Pass |
| Unit tests | 764 pass (51 new) |
| Unit + integration with PostgreSQL and Redis | 1115 pass (68 new) |
| End to end with nothing faked (`make be-test-browser`) | 22 pass (1 new): real PNG, WebM and trace pass the content checks; report and all exports |
| Engine suite, runner suite | 98 and 11 pass, unchanged |
| Frontend: ESLint, Prettier, vue-tsc, build, Vitest | Pass; 74 tests (13 new) |
| Live run through the running stack (real queue, worker, engine, demo application) | Three executions; report totals matched; PDF, CSV and JSON exported; download address served the screenshot without a session header while the plain address answered 401; the five pages rendered in light and dark themes |

Test coverage asked for in the brief:

| Area | Tests |
|------|-------|
| Report accuracy | Report totals equal the execution's own totals and stored statuses for a run with every outcome; pure assembly test with one result of each status; pass rate excludes skipped and blocked; expired evidence left out |
| Evidence permissions | Viewer can read; same-organization non-member, other organization and anonymous cannot; not through another execution or project; download address ends when the user is removed or signs out; forged, expired and wrong-kind tokens |
| Exports | JSON equals the report; CSV rows, columns and formula neutralisation; PDF content, fonts, broken image; headers; unknown format; audit entries |
| File validation | Fifteen refused combinations (wrong signature, wrong type, unknown kind, empty, too large); through the worker: invalid, empty, oversized, over quota |
| Execution history | Order, paging, filters, limits; case history; failed tests; comparison of every kind of change; trend points; dashboard definitions |

### Deviations from the plan

| Plan | Built | Why |
|------|-------|-----|
| Reports stored in `test_reports` in each format | Generated on request | Always consistent with the data; no migration; nothing to invalidate |
| HTML and JUnit report files, share links | Not built | The brief asks for web, JSON, PDF and CSV; share links would be a public route to evidence |
| Object storage | Interface only | No object store is in the stack |
| Per-project retention | Per installation, by kind | Projects have no settings API yet |

### Known limitations

- One storage implementation (a directory).
- Screenshots and recordings are not scanned for sensitive content.
- PDF replaces characters outside Latin-1.
- History figures are computed on request from at most 20,000 rows per period.
- A download address can be reused within its five minutes by whoever holds it, while its user keeps access.
- Starting executions, workflows and heal proposals still have no pages.
- Still never run against a live model (affects only the AI suggestion shown in reports).

### Decisions taken in Phase 10

| Decision | Reason |
|----------|--------|
| Totals in a report come from the function that decides the verdict | A report cannot disagree with its execution |
| Pass rate counts only passed results, over cases that ran | Flaky and needs-review are not passes; skipped cases say nothing |
| No rate when nothing ran | 0% and 100% would both be false |
| "Test coverage indicators", never "coverage" alone | No code instrumentation is integrated |
| Download tokens are bound to user and session and re-checked | A link must not outlive the access it was given with |
| A refused evidence file does not change a result | Evidence supports a verdict; it does not make one |

### Carried into Phase 11

- Reports and history are the natural source for notifications and for CI output.
- Pages for running tests, workflows and heal proposals are still missing.

## Phase 11 — GitHub and CI/CD Integration

Design: [github-and-ci.md](github-and-ci.md).

### Delivered

**GitHub**
- Connections: a fine-grained personal access token or a GitHub App installation, checked with GitHub when stored, encrypted, never returned.
- Linked repositories: one repository per link, by explicit choice, with GitHub's permanent ID recorded and re-checked.
- Reading from GitHub for linked repositories: repository selection, metadata, branches, pull request information with changed files, commit information.
- Webhook endpoint with per-link secret, signature verification, duplicate detection, size and rate limits.

**Pull request testing**
- Opened, reopened, ready-for-review and new-commit events start the link's approved suites in its environment, for configured branches only. Pushes to configured branches as a separate switch.
- One execution per suite and commit, however many events arrive.

**CI/CD**
- API keys (shown once, hashed, scoped, optionally per project, expiring, revocable).
- CI endpoints: start, status with verified outcome and exit code, JUnit XML, full report.
- A one-file command-line script and a GitHub Actions example workflow.

**Test selection**
- Path rules map changed files to suites. A run is narrowed only when every changed file is covered and GitHub's list is complete; otherwise the full set runs. No model.

**Pull request reporting**
- Commit status per suite and one comment per pull request, each behind its own switch, off by default.

**Other**
- Migration `0009` (two tables); permission `integrations:manage` for organization admins; executions can now be started by a key or by the system, with the same rules as for a person.

### Found while building it

| Found | What was done |
|-------|---------------|
| API keys and webhook addresses could not be looked up at all: both tables are under row-level security, and a request with only a key or a hook token has no organization yet | The organization is part of the key (`qak_<organization>_<secret>`) and of the webhook address, so the lookup happens inside that organization's scope |
| A backslash before `@` is not a dependable way to stop GitHub from notifying a user named in a comment | A zero-width space follows every `@` in text taken from test runs |
| The first version of the comment test accepted escaped HTML as "absent" by substring | The test now checks that no unescaped markup remains |

### Validation performed

| Check | Result |
|-------|--------|
| Ruff, format, mypy strict (260 files) | Pass |
| Unit tests | 854 pass (90 new) |
| Unit + integration with PostgreSQL and Redis | 1245 pass (130 new); migration round trip included |
| End to end with real browsers (`make be-test-browser`) | 22 pass, unchanged: the execution start path was restructured and still behaves the same |
| Engine, runner and frontend suites | Not re-run; nothing they cover was changed |
| Live: the CI script against the running stack (real API, queue, worker, engine, demo application) | Passing suite: exit 0. Suite with the seeded defect: exit 1, the failing case named, JUnit written. Wrong key: exit 4. The key appeared in no output |
| Against GitHub itself | **Not done.** No GitHub credentials were available |

Test coverage asked for in the brief:

| Area | Tests |
|------|-------|
| Webhook verification | Twelve refused signatures (wrong secret, SHA-1, altered or re-serialised body, truncated, wrong case); through the API: eight refused deliveries leave no row and no job; valid signature at another link's address; rotation; headers; size |
| PR event handling | Four triggering actions; ignored actions, branches, drafts, closed, switched-off triggers and links, another repository, other event types; pushes; new commits |
| Duplicate events | Same delivery ID; a different event for the same commit; the job delivered twice; an event overtaken by a newer commit |
| Execution triggering | Suites, environment, trigger, actor and git context of started executions; selection by path rules and its fall-back to everything; approval-required environments; CI start, scopes, project limits, idempotency |
| CI status reporting | Outcome and exit code for every verdict and lifecycle state; commit status states; JUnit counts; the script's exit code for ten answers including contradictory ones |
| GitHub API failures | Ten HTTP failures, timeouts, unreachable host, unreadable answers, a redirect; in the flow: files unreadable (everything runs), status and comment refused (result unchanged, failure recorded), GitHub down while connecting |

### Deviations from the plan

| Plan | Built | Why |
|------|-------|-----|
| GitHub App installation flow with install URL and callback | Credentials entered by an admin (token, or app ID + key + installation ID) | The flow needs a registered app and a public callback address, which a self-hosted installation may not have. Both authentication mechanisms GitHub supports are implemented |
| Check runs | Commit statuses | Check runs require a GitHub App; statuses work with either mechanism |
| One global webhook endpoint | One address and secret per linked repository | A leaked secret affects one repository, and the address places the lookup inside one organization |
| `flaky` exits 0 with a warning | `flaky` exits 2 unless `--allow-flaky` | A pass on retry is not a verified pass; tolerating it is the pipeline owner's explicit choice |
| CI runs needing approval wait as `awaiting_approval` | Refused with `approval_required` | Nothing exists yet to grant that approval later |
| `deployment_status` trigger, opening issues | Not built | Not in this phase's brief |

### Known limitations

- Never exercised against GitHub itself; paths, headers and payload fields follow GitHub's documentation.
- The webhook tests whatever is deployed on the configured environment; the platform does not deploy a pull request.
- No web pages for connections, links or API keys.
- Impact is known only from path rules.
- A commit that already has executions is not run again by the webhook.
- GitHub only.

### Decisions taken in Phase 11

| Decision | Reason |
|----------|--------|
| An event selects nothing | Suites, environment and branches come from the link, so a forged or unexpected payload cannot choose what runs |
| Unknown address and wrong signature get the same answer, and neither is stored | No confirmation of addresses, no storage to fill |
| Only `passed` is success, everywhere | CI status, commit status and exit code all come from one function over the verdict |
| Narrow a run only when every changed file is covered by a rule | An unknown file is a reason to test everything |
| Automated triggers cannot approve | Approval is a person's decision |
| Publishing failures never change a result | Reporting is not verification |

### Carried into Phase 12

- Deliveries, publishing outcomes and CI usage are recorded and ready for notifications and monitoring.
- Pages for running tests, workflows, heal proposals and now integrations are still missing.
- A first run against GitHub itself is the open risk of this phase.

## Phase 12 — Complete Frontend Dashboard

Design: [frontend.md](frontend.md).

### Delivered

- **New screens:** password reset (request and confirm), organization dashboard, execution in progress (live), replacement locators, integrations (GitHub, CI keys, browser service), project GitHub repositories, settings (profile, security, organization and members, roles, audit log).
- **Extended screens:** start an execution from the suite page or the history page; the report page gained AI analysis on request, a log viewer and an enlargeable screenshot viewer; sign-in and registration land on the dashboard.
- **Reusable components:** StatCard, DataTable, FilterBar, PaginationBar, TabList, CodeViewer, ScreenshotViewer, ExecutionProgress, ResultSummary, StartExecutionDialog, beside the existing dialogs, forms and state components.
- **Live updates:** server-sent events. The worker publishes what the engine reports to Redis; the API streams it; the page reconnects from the last entry and falls back to polling.
- **Backend additions for the interface:** `GET /organizations/{id}/dashboard`, `GET …/executions/{id}/live`, `GET …/executions/{id}/events`, `GET /meta/roles`.

### Found by running it

| Found | What was done |
|-------|---------------|
| Registration and sign-in still landed on the project list, not the new dashboard | Both now go to the dashboard; the safe-redirect default changed with them |
| A stream that failed before any state arrived showed a dead error page although the page was reconnecting | The page shows "reconnecting" until it has given up; a missing or forbidden execution is not retried at all |
| Repeated walk-throughs hit the password-reset rate limit | Correct behaviour; the walk-through accepts either answer |

### Validation performed

| Check | Result |
|-------|--------|
| ESLint, Prettier, vue-tsc, production build | Pass |
| Frontend tests | 90 pass (16 new) |
| Backend: Ruff, mypy strict (261 files); unit + integration | Pass; 1247 pass (2 new, for live progress and the organization dashboard) |
| Browser walk-through of the running stack (`make e2e`) | 173 checks pass: 26 screens at 1366 px and 820 px, a suite run from the interface and watched live, report, screenshot, PDF and CSV downloads, navigation, sign-out |

### Deviations and limitations

- No pages for agent workflow runs; the same steps exist as separate screens.
- Path rules are edited through the API; the profile is read-only.
- The walk-through runs as an owner. Role-dependent behaviour is covered by component tests and by the API's tests.
- Live progress shows steps as they finish; the engine does not report a step before it ends.

### Decisions taken in Phase 12

| Decision | Reason |
|----------|--------|
| Server-sent events over `fetch`, not `EventSource` or WebSockets | One-way updates; `fetch` can send the bearer token; no new dependency |
| Progress is cases finished over cases selected, nothing finer | Anything finer would be an estimate |
| The role table is fetched, not copied | One source of truth for permissions |
| Tablet keeps the sidebar; the drawer starts below 769 px | The content still fits; fewer taps |

## Phase 13 — Security, Reliability and Production Safeguards

Design and status: [security-design.md](security-design.md) §16–17; operations: [operations.md](operations.md).

Most of what this phase asks for had been built with the features it protects (allowlisting and SSRF defence, browser isolation, prompt hardening, encrypted credentials, tenant isolation, rate limits, audit, workflow recovery). This phase audited each requirement, filled the gaps and added tests that hold the whole in place.

### Delivered

- **Sensitive operations.** Steps that delete, pay, send messages, change configuration or clean up destructively are recognised by rule. They need a person's approval for that execution, are never run against production, and cannot be approved by a key, a webhook or a workflow. The start dialog shows them and asks.
- **HTTP hardening.** Security headers on every answer, HSTS in production, a request-size limit.
- **Second redaction.** The worker now removes secrets from engine events itself, instead of relying on the engine having done so.
- **Recovery of executions.** An execution whose worker stopped is closed by a scheduled job and never run twice.
- **Health checks.** The worker checks the engine and the browser runner every minute; the result is visible through the API.
- **Metrics.** A token-protected Prometheus endpoint covering API errors, agent failures, LLM latency and tokens, MCP latency, execution duration, queue delay, test results and process resources.
- **Sweep tests** over every route: tenant isolation, anonymous access, what a viewer can change, and where a stored secret can appear.

### Found by testing

| Found | What was done |
|-------|---------------|
| A secret echoed by the application under test (for example in a console message) was kept out of stored data only by the engine's redaction. A faulty or compromised engine would have let it through | The worker redacts every engine event with the secrets it handed out, before storing or publishing anything. The sweep test uses an engine that does not redact |
| Treating every state-changing step whose label contains a sensitive word as sensitive flagged typing into a field called "Email" | Only steps that commit something (click, select, API call) are examined; typing is not |

### Validation performed

| Check | Result |
|-------|--------|
| Ruff, mypy strict (266 files) | Pass |
| Unit tests | 882 pass (28 new) |
| Unit + integration | 1285 pass (38 new) |
| Real-browser end to end | 22 pass |
| Browser walk-through of the web application | 173 checks pass |
| Frontend | 90 pass |
| Live | The worker's health checks report the real engine and runner as healthy |

Tests asked for in the brief:

| Area | Tests |
|------|-------|
| SSRF, URL redirects | Existing suites for URL safety, the egress proxy, application registration and the real browser; unchanged and passing |
| Tenant isolation | Sweep over every organization-scoped route (more than 100) as an outsider, anonymously, and with the outsider's own organization in the address |
| Credential exposure | Sweep over every GET route, every export, every evidence file and the audit log for a stored secret, with an engine that leaks it |
| Malicious page content | Existing prompt-fencing and "model claims a pass" tests |
| Unauthorized actions | Sweep: a viewer through every mutating route; sensitive-step approval by role; production; automated triggers |
| Resource exhaustion | Oversized bodies by declared and actual size, unbounded list requests, existing rate-limit and quota tests |
| Execution cleanup | Nothing left in the engine, the proxy or Redis after a run; recovery after a dead worker; a job delivered twice |

### Remaining risks

Listed in [security-design.md](security-design.md) §17. The main ones: the browser's own sandbox is off inside its container; sensitive steps are recognised by English wording; evidence images are not scanned; nothing has run against a live model or GitHub; a single worker and engine.

## Phase 14 — Complete Testing, Evaluation and Deployment

Guides: [installation.md](installation.md), [deployment.md](deployment.md), [testing-guide.md](testing-guide.md), [evaluation-results.md](evaluation-results.md), [known-limitations.md](known-limitations.md).

### Delivered

- **Demonstration scenarios.** The demo application gained registration, a product catalogue with an API, and a responsive page. Twelve scripted scenarios cover login (good and bad), registration, form validation, product creation, invalid input, a business rule, API response validation, responsive layout and navigation. Each states the verdict and failure category it must end with; three must not pass. They run in the engine alone and through the whole platform.
- **The complete workflow test.** One test takes the fifteen steps of the brief in order — registration to execution history — with real PostgreSQL, Redis, worker, MCP server, engine, proxy and browser. Only the model is a stand-in.
- **Evaluation harness** (`make evaluate`): labelled datasets for failure classification, locator recovery, sensitive-step detection, test selection and tool selection, with false positives and false negatives reported separately; 20 labelled instructions for the model-based metrics, which run only when a model is configured.
- **Outgoing mail.** An SMTP sender for invitations and password resets (STARTTLS, or TLS on port 465); bodies are never logged. Without a host, behaviour is as before.
- **Production configuration.** `docker-compose.prod.yml` with its own project name, production image targets (a new one for the engine without development dependencies), read-only filesystems, dropped capabilities, no published data-store ports, log rotation, required secrets that stop the start when empty; `.env.production.example`; nginx with a content security policy, an unbuffered route for live progress and no route to `/metrics`.
- **CI additions.** Jobs for the browser walk-through and evaluation, for dependency and secret scanning, and for building the production images and checking that the production configuration refuses empty secrets. No deployment job.
- **Documentation.** README with an index, installation, deployment (secrets, migrations, monitoring, backup and restore, a GCP-compatible layout), testing guide, evaluation results, known limitations.

### Found by testing

| Found | What was done |
|-------|---------------|
| A script using `assert_api_json` with `type`, `text` or `exists` was accepted by the API and refused by the engine once written out in full, because an explicit `equals: null` was read as "expects null" | The validator counts `equals: null` only when nothing else is given. A unit test now writes every script form out in full and reads it back |
| When the engine refused a plan, the run ended with an unexplained error | The refusal is reported as `engine_rejected_plan` with the location of the problem (never the values) and classified as a test-automation failure |
| The evaluation found two unsafe locator recoveries: "Pay 10.00" to "Pay 100.00", "Message Anna" to "Message Hanna" | Recovery now refuses a replacement in which any number differs or a word was replaced by another. Precision on the dataset went to 100%; one safe abbreviation is now missed |
| "Archive" was not recognised as a destructive word | Added |
| Behind nginx, API answers carried two content security policies | The proxy locations no longer add the web server's own |
| The development stack did not pass the mail settings to the containers | Added to `docker-compose.yml` and `.env.example` |

### Validation performed

| Check | Result |
|-------|--------|
| Ruff, format, mypy strict (268 files) | Pass |
| Models and migrations agree (`alembic check`) | Pass; no schema change in this phase (39 tables, revision 0009) |
| Unit tests | 884 pass |
| Unit + integration | 1287 pass |
| Backend coverage (line and branch, unit + integration run) | 83% of `backend/app` (16,851 statements, 3,418 branches). Measured without the engine and real-browser suites, which run in other containers, so engine code counts as less covered than it is. No target is enforced |
| Engine tests in real browsers | 125 pass |
| Real-browser end to end, including the twelve scenarios and the fifteen-step workflow | 24 pass |
| Runner tests | Pass |
| Frontend lint, types, tests; production build | 90 pass; builds |
| Browser walk-through of the web application, development stack | 173 checks pass |
| Browser walk-through against the production images | 173 checks pass |
| `pip-audit`, `npm audit --omit=dev --audit-level=high`, gitleaks | No known vulnerabilities; no leaks |
| Production compose with empty secrets | Refuses to start, naming the variable |

Evaluation (rule-based components only):

| Component | Cases | Result | False positives | False negatives |
|-----------|------:|--------|-----------------|-----------------|
| Failure classification | 34 | accuracy 91.2% | 0 | 1 |
| Self-healing | 32 | precision 100%, recall 91.7% | 0 | 1 |
| Sensitive-step detection | 40 | 22 of 24 found | 1 | 2 |
| Test selection | 14 | accuracy 100% | — | 0 |
| Tool selection | 15 | accuracy 100% | — | — |

Requirement understanding, scenario coverage, structured-output validity, expected-result accuracy, token usage, latency and cost: **not measured**. No model was available.

### Production rehearsal

On 2026-11-20 the production images were built (web 74 MB, backend 844 MB, runner 1.7 GB, engine 4.2 GB) and the production stack was started on one host with generated secrets. Every service became healthy on read-only filesystems; the API documentation answered 404; HSTS was present; `/metrics` was not reachable through the web server. The walk-through passed all 173 checks. The stack and its volumes were then removed.

This was a rehearsal on a development machine. **Nothing has been deployed**, and no deployment is automated.

### Deviations

- **Deployment.** The brief asks for production deployment configuration and, if a cloud deployment is required, a compatible architecture. Both are delivered as configuration and documentation. No deployment was performed: none was authorised and no target exists.
- **AI evaluation.** The harness and dataset exist; the model-based half has not run.
- **Security scanning** covers dependencies and secrets. Container images are not scanned for operating-system vulnerabilities.
- **Backup and restore** are documented, not rehearsed.

### Limitations

See [known-limitations.md](known-limitations.md). In short: never run with a live model, against GitHub, on GitHub Actions or in production; one host, one worker; sensitive steps recognised by English wording, with two of 24 missed in evaluation.

### Addendum (2026-11-20): Gemini as a model provider

Requested after the phase report. The AI service can now be OpenAI or Gemini, chosen with `LLM_PROVIDER`; Gemini uses Google's native API through `langchain-google-genai` (the approach of the sibling `mcp` project), not its OpenAI-compatible endpoint.

- `GeminiGateway` inherits the whole gateway contract (validated structured output, one repair, counted retries, call records, messages without provider text) and replaces only the client, the structured-output request and the reading of Google's errors, including the wait Google states in a 429 and answers cut off or blocked by safety filters.
- New settings: `LLM_PROVIDER`, `GEMINI_API_KEY`, `LLM_REQUESTS_PER_MINUTE` (client-side pacing per process, for rate-limited keys). Only the chosen provider's key is read.
- **Deviation from the brief:** the mandatory stack names the OpenAI API. OpenAI remains supported and is the default; Gemini was added at the user's request and adds one dependency.
- Validation: 913 unit tests pass (29 new); unit + integration 1316 pass; Ruff and mypy pass (270 files). One live requirement analysis and the full `make evaluate` ran against `gemini-3.5-flash-lite`.
- First real-model figures ([evaluation-results.md](evaluation-results.md) §4): structured output valid 100%; no invented rules; both prompt injections resisted; never failed to ask when it should; **but only 45% of clarification decisions correct, because it asked needlessly on 11 of 20 instructions.** Only 4 instructions reached case generation.
- Not done: prompt tuning for the over-asking; a run of the whole agent workflow with the real model; pricing for cost tracking.

### Addendum (2026-11-20): analysis prompt tuned and re-measured

The first Gemini run showed the analysis asking a blocking question on 11 of 20 instructions that did not need one. Cause: prompt v1 demanded a question for every expected result not spelled out.

- **`requirement_analysis.v2.md`** (v1 kept; the version is recorded with every call). Outcomes fixed by the instruction's wording count as stated; a question is asked only when a correct test cannot be written without the answer; observable details are not asked about; a bare "works" or "is correct" states no outcome. The rule against inventing results, rules and values is unchanged.
- **Result** ([evaluation-results.md](evaluation-results.md) §4): clarification decisions correct 45% → 90% (18 of 20); asked needlessly 11 → 2; did not ask when needed 0 → 0; invented rules 0; injections resisted 2 of 2; instructions with generated cases 4 → 11.
- **A first draft of v2 was worse in one respect and was corrected:** it stopped asking about "make sure checkout works", and the model copied a concrete example from the prompt into an unrelated analysis.
- **Bug found and fixed:** an answer from Gemini that did not fit the schema skipped the gateway's repair attempt and ended as an unexplained error. Three unit tests added.
- **Evaluation harness:** provider outages are counted separately from invalid output; each instruction's questions are kept in the JSON result.
- Validation: unit 916 pass; unit + integration 1319 pass; Ruff and mypy pass.
- Open: half of the generated cases have no sourced expected result (generation prompt untuned); Gemini's free tier was unreachable for 2 of 31 calls; single runs on 20 instructions.

### Addendum (2026-11-20): demo data and the first real-model workflow

- **`make demo`** (`scripts/demo.sh`, `scripts/seed_demo.py`): through the public API only, creates a login, a project against the bundled demo application, an environment with a test login, three approved cases in an approved suite and one execution. Development stacks only; the login's password is generated into `.env`.
- **Retry settings** `LLM_MAX_ATTEMPTS`, `LLM_RETRY_BASE_DELAY_S`, `LLM_RETRY_MAX_DELAY_S` existed but were not passed to the containers; they are now, and documented in the templates.
- **First run of the whole agent workflow with a real model** (Gemini `gemini-3.5-flash-lite`), from one plain-language instruction about login on the demo shop:
  - With three attempts per call the run failed at case generation: the provider answered "high demand" three times. With six attempts and longer waits it went through (4 of 8 model calls had to be retried).
  - Analysis, read-only inspection through Playwright MCP (12 calls, none refused), case generation, script generation, the approval stop, execution in a browser, failure analysis and the report all completed.
  - **Both generated tests failed, and the failures are the tests' fault, not the application's.** The model wrote correct steps (open the page, fill both fields with the stored test login, submit) but pointed the final text check at an element it had seen on the login page instead of where the result appears; it found "Sign in" where it expected "Signed in as". The read-only inspection never sees the page after login. The platform reported both as failed (`test_assertion_failure`) and did not pass anything unverified. The stored steps show the credential placeholders, not the values.
  - One run, one instruction. It shows the path works end to end and where the generated scripts are weak; it is not a measurement.
