# QA Agent

An AI-powered testing platform: describe what to test in plain language, and the
platform plans, runs and reports browser and API tests.

**Current state: all fourteen build phases are complete and tested; the platform
has not been deployed to production and has not yet run against a live language
model or against GitHub itself.** What that means precisely is in
[docs/known-limitations.md](docs/known-limitations.md). A started stack is not
proof of readiness: read that page before relying on the platform.

## Documentation

| To do this | Read |
|---|---|
| Install on a development machine, set up the environment | [docs/installation.md](docs/installation.md) |
| Run in production, secrets, migrations, backups, cloud layout | [docs/deployment.md](docs/deployment.md) |
| Monitor, recover, rotate keys | [docs/operations.md](docs/operations.md) |
| Run the tests; what each suite proves | [docs/testing-guide.md](docs/testing-guide.md) |
| See what was measured, and what was not | [docs/evaluation-results.md](docs/evaluation-results.md) |
| Know what does not work or is unverified | [docs/known-limitations.md](docs/known-limitations.md) |
| Understand the design | [docs/architecture.md](docs/architecture.md), [docs/database-design.md](docs/database-design.md), [docs/api-design.md](docs/api-design.md), [docs/security-design.md](docs/security-design.md) |
| Understand the agent and the browsers | [docs/agent-workflow.md](docs/agent-workflow.md), [docs/playwright-mcp-integration.md](docs/playwright-mcp-integration.md), [docs/test-execution-engine.md](docs/test-execution-engine.md), [docs/result-validation-and-healing.md](docs/result-validation-and-healing.md) |
| Reports, evidence, GitHub and CI, the web application | [docs/reports-and-evidence.md](docs/reports-and-evidence.md), [docs/github-and-ci.md](docs/github-and-ci.md), [docs/frontend.md](docs/frontend.md) |
| What was built in each phase, and how it was checked | [docs/progress.md](docs/progress.md) |

## What it does

One request takes an instruction all the way to a report: the
workflow analyses the requirement (asking questions if it is unclear), looks at
the application read-only, writes test cases and their executable scripts, waits
for a person to approve them, runs them with the deterministic engine, checks
the results, retries what failed for transient reasons, and stores a report.

Every result that does not pass is put in a category by rules (application,
assertion, test automation, browser infrastructure, network, authentication,
inconclusive), so a browser crash is not filed as an application bug. A model
can add a summary, a possible cause and next steps, always labelled as a
suggestion. When a locator no longer matches, the engine can find the renamed
element under strict rules; such a run is `needs_review`, never `passed`, and
the replacement reaches the script only after a person accepts and approves it.
Unstable tests are reported as flaky and stay visible. A model never decides
whether a test passed.

Every execution has a test report (web page, JSON, CSV, PDF) counted from the
stored results, with evidence: screenshots, optional video and trace, console
and network logs. Evidence is checked before it is stored, kept for a limited
time and served only to people who can see the project. Execution history,
comparison of two runs, trends and a dashboard are available in the web
application. See [docs/progress.md](docs/progress.md),
[docs/agent-workflow.md](docs/agent-workflow.md),
[docs/result-validation-and-healing.md](docs/result-validation-and-healing.md) and
[docs/reports-and-evidence.md](docs/reports-and-evidence.md).

Approved tests can run from a development workflow: from a pipeline with an API
key (a one-file script and a GitHub Actions example are included), or
automatically when a pull request on a linked GitHub repository is opened or
updated. Only a verified pass is reported as success. Nothing is triggered and
nothing is written to GitHub unless it has been switched on for that
repository. See [docs/github-and-ci.md](docs/github-and-ci.md).

The web application covers the whole path: projects, applications and test
credentials, instructions, test cases and suites, starting a run and watching
it live, reports, history, integrations and settings. See
[docs/frontend.md](docs/frontend.md).

## Requirements

- Docker with Compose v2
- `make`

Nothing else needs to be installed on the host: Python 3.12 and Node 22 run in
containers. The first `make up` downloads the Playwright MCP image (about
1.7 GB) and the Playwright image with three browsers for the test engine (about
3.5 GB).

## Quick start

```bash
make up        # creates .env on first run, builds images, starts everything, waits until healthy
```

Then open:

| What | URL |
|------|-----|
| Frontend | http://localhost:5173 |
| API docs (development only) | http://localhost:8000/api/docs |
| Liveness | http://localhost:8000/healthz |
| Readiness (PostgreSQL + Redis) | http://localhost:8000/readyz |

To have something to look at straight away, `make demo` creates a login, a
project against the bundled demo shop, three approved tests and one finished
run (see [docs/installation.md](docs/installation.md) §4).

If a port is already taken, edit the `*_HOST_PORT` values in `.env` and run
`make up` again.

```bash
make ps        # service status
make logs s=api
make down      # stop; data is kept
make clean     # stop and delete the database and Redis volumes
```

## Configuration

All configuration is read from environment variables. `make env` creates `.env`
from [.env.example](.env.example) and generates the secrets (two database
passwords, the token signing key, the credential master key and the three tokens of
the browser and engine services). It is safe to re-run: existing values are
kept and only missing secrets are added. `.env` is git-ignored; never commit it.

The backend refuses to start if `DATABASE_URL`, `REDIS_URL`, `JWT_SECRET_KEY`
or `CREDENTIAL_MASTER_KEYS` is missing — there are no built-in defaults for secrets. Docker Compose builds
the connection strings from the values in `.env`.

### Database roles

Two PostgreSQL roles are used, so that tenant isolation is enforced by the
database and not only by application code:

| Role | Variable | Used by |
|------|----------|---------|
| Schema owner | `POSTGRES_USER` | The migration job only. |
| Application role | `APP_DB_USER` | API and worker. Cannot bypass row-level security or run DDL. |

The migration job creates the application role automatically. See
[docs/database-design.md](docs/database-design.md) §8.

### AI service

The AI steps need a model from one of two providers. Set these in `.env`:

```
# OpenAI
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
LLM_MODEL_ANALYSIS=<a model that supports structured outputs>

# or Gemini (Google's native API)
LLM_PROVIDER=gemini
GEMINI_API_KEY=...
LLM_MODEL_ANALYSIS=<a gemini-* model>
LLM_REQUESTS_PER_MINUTE=6     # pacing per process; the API and the worker count separately
```

Only the chosen provider's key is read. Do not point `OPENAI_BASE_URL` at
Gemini's OpenAI-compatible endpoint; use `LLM_PROVIDER=gemini`.

There is deliberately no default model. Without a key everything else works and
AI endpoints answer `503 llm_not_configured`. Optional: token prices
(`LLM_INPUT_PRICE_PER_MTOK`, `LLM_OUTPUT_PRICE_PER_MTOK`) to record cost per
call, and `LLM_MONTHLY_BUDGET_USD` to cap spending per organization. After
changing `.env`, run `make up`.

What is sent to the provider: the instruction text, clarification answers, and
names and settings of the application and environment. Credential values and
variable values are never sent.

### Credential master key

Test credentials you store are encrypted with `CREDENTIAL_MASTER_KEYS`. **Back
this value up separately from the database**: without it, stored credentials
cannot be decrypted, and there is no recovery. To rotate, append a
higher-numbered key (`1:…,2:…`); new and re-saved credentials use the newest.

### Browser automation

Three services work together, and `make up` starts all of them:

| Service | Role |
|---------|------|
| `worker` | Decides what a browser session may do and talks MCP to it. |
| `runner` | Starts one Playwright MCP server with its own browser per session, and destroys it afterwards. It is on an internal network with no route to the databases or the internet. |
| `egress-proxy` | The browser's only way out. It connects only to the origins the environment allows. |
| `engine` | Runs approved test scripts with Playwright, one process per run. Same isolation as the runner. |

To try it against the bundled demo application:

```bash
docker compose --profile demo up -d demo-app
```

Register an application and an environment with the address
`http://demo-app:8080`, with "Allow localhost and private network addresses"
switched on (it is a container address). Add a credential (`demo` /
`demo-password`) with a login recipe, then start a check:

```
POST /api/v1/organizations/{org}/projects/{project}/applications/{app}/environments/{env}/browser-checks
{ "credential_id": "…" }
```

and poll the returned check. The recipe format, the tool permissions and
everything else are in
[docs/playwright-mcp-integration.md](docs/playwright-mcp-integration.md).

### The agent workflow

```
POST …/projects/{project}/workflows           { "application_id": "…", "environment_id": "…", "text": "Test the customer login…" }
GET  …/projects/{project}/workflows/{id}      status, what it waits for, history, usage
POST …/workflows/{id}/clarifications          answers, when it asks questions
POST …/workflows/{id}/approval                { "decision": "approve" }   (QA engineers and admins)
GET  …/workflows/{id}/report                  the final report
```

It needs the AI service (`OPENAI_API_KEY` and `LLM_MODEL_ANALYSIS`), the engine
and, for looking at the application first, the browser runner. While a run waits
for approval, the generated cases and scripts can be read and edited through the
ordinary test-case endpoints. A run that changes data on an environment marked
as requiring approval, or targets production, can only be approved by an
organization admin.

If a worker stops while a run is in progress, another worker (or the same one
after a restart) continues it from its last checkpoint within about a minute. A
test that was in the middle of running is not blindly run again.

To run an approved suite again without generating anything, start a workflow
with `suite_id` instead of `text`, or start an execution directly as below.

### Running tests directly

A test case becomes runnable when it has a script and is approved:

```
PUT  …/projects/{project}/test-cases/{case}/automation     the script (steps, locators, assertions)
POST …/projects/{project}/test-cases/{case}/approve
POST …/projects/{project}/executions                       { "environment_id": "…", "case_ids": ["…"] }
GET  …/projects/{project}/executions/{id}                  status, verdict, results
GET  …/executions/{id}/results/{result}/steps              every step, expected and actual
GET  …/executions/{id}/evidence                            screenshots, videos, traces
```

The step types, locators, assertions, test data, configuration and the rules by
which a case and an execution are judged are in
[docs/test-execution-engine.md](docs/test-execution-engine.md). In short: a
step that cannot be carried out is an *error*, an assertion that does not hold
is a *failure*, and an execution only passes if every selected case ran and
passed.

Use made-up data in test data sets and test files. Real customer data must not
be used for testing.

Evidence files are kept on the `evidence` volume. They are marked with an expiry
date, but nothing deletes them yet.

The Playwright MCP version is pinned in [runner/Dockerfile](runner/Dockerfile).
To upgrade it, follow section 10 of that document; do not just change the tag.

### Testing applications on localhost or a private network

By default the platform refuses to contact loopback, private and cloud-metadata
addresses, so that a URL typed into a form cannot be used to reach internal
systems. To test an application on such an address, an organization admin must
tick "Allow localhost and private network addresses" on that environment.
Remember where addresses are resolved: by the API container when an environment
is saved, and by the egress proxy when a browser opens it. Use a name both can
reach, not the `localhost` of your own machine. Loopback addresses and the
platform's own internal network are never reachable from a browser session,
whatever is switched on.

### Emails in development

Without `SMTP_HOST` in `.env` no mail is sent: password-reset and invitation
emails are written to the API log instead (`make logs s=api`, look for
`email_not_delivered_dev_mode`); copy the link from there. In production mode
the body is not logged.

Frontend build-time settings are described in
[frontend/.env.example](frontend/.env.example). Anything prefixed `VITE_` ends up
in the browser bundle, so it must not be secret.

## Development

Source directories are mounted into the containers, so edits reload
automatically (Uvicorn `--reload`, Vite HMR).

| Task | Command |
|------|---------|
| Every lint, type check and unit test | `make check` |
| Backend: lint / format / types / tests | `make be-lint` · `make be-format` · `make be-typecheck` · `make be-test` |
| All backend tests, including auth, permissions and tenant isolation | `make be-test-integration` (stack must be up) |
| Browser tests: real Playwright MCP server, browser, proxy and demo app | `make be-test-browser` (stack must be up) |
| Test engine: every action, locator and assertion in real browsers | `make engine-test` |
| Tests of the browser runner | `make runner-test` |
| Walk through the web application in a real browser | `make e2e` (stack must be up) |
| Measure the rule-based components on labelled data | `make evaluate` |
| Frontend: lint / format / types / tests | `make fe-lint` · `make fe-format` · `make fe-typecheck` · `make fe-test` |
| Apply migrations | `make migrate` |
| Create a migration | `make migration m="describe the change"` |
| Refresh `uv.lock` after editing `pyproject.toml` | `make be-lock`, then `make build` (this also rebuilds the engine image, which shares the backend's dependencies) |
| Shell in a container | `make be-shell` · `make fe-shell` |

Adding a frontend package: `make fe-shell`, then `npm install <package>`; commit
`package.json` and `package-lock.json`, then `make build`.

`make be-test` runs only the tests that need no services; browser behaviour is
tested there against a mock MCP server. `make
be-test-integration` runs everything against a scratch database (`<name>_test`,
recreated and migrated on every run) and Redis database 15; your development
data is not touched.

Run `make help` for the full list.

### Production

Production uses its own compose file, images and settings template, and shares
nothing with the development stack. Nothing deploys automatically. See
[docs/deployment.md](docs/deployment.md).

### Before committing

Run `make check`. If you use [pre-commit](https://pre-commit.com), a local hook
that runs `make be-lint be-format-check fe-lint fe-format-check` covers the fast
checks; type checks and tests are left to `make check` and CI because they are
slower.

## Layout

```
backend/
  app/
    main.py            application factory
    api/               routers, dependencies, exception handlers (v1/ = versioned API)
    core/              settings, logging, errors, error envelope, Redis client, rate limiting
    core/security/     passwords, tokens, roles and permissions, credential encryption, URL safety
    db/                engine, session factory, tenant context, error mapping, role bootstrap
    middleware/        request ID, access log, last-resort error handling
    models/            SQLAlchemy models
    schemas/           Pydantic request/response models
    repositories/      data access
    services/          business logic (route handlers stay thin)
    worker/            arq worker and background jobs
    agent/             AI module: LLM gateway, prompts, output schemas, the model calls
                       (analysis, cases, scripts, failure explanations), untrusted-text guard
    testing/           structured test cases and their checks; executable scripts, their
                       pre-run checks, business-rule expressions, verdict rules
    mcp/               MCP client: connect, discover, validate arguments, call, close
    browser/           browser sessions: tool permissions, allowed addresses, redaction,
                       login, session lifecycle, runner and proxy clients
    egress/            the egress proxy (its own process)
    workflow/          the LangGraph agent workflow: state, nodes, edges, checkpoints,
                       discovery, retry rules, report, runner
    engine/            the test engine: browser, locators, actions, assertions, evidence,
                       run loop, and the service around it (its own image and process)
    utils/
  alembic/             migrations
  tests/unit/          no network
  tests/integration/   real PostgreSQL / Redis / worker
frontend/
  src/
    api/               fetch client and endpoint functions
    components/ui/     reusable primitives (button, modal, form field, states)
    components/domain/ application, environment and credential forms; environment card
    composables/  config/  errors/  layouts/  pages/  router/  stores/  styles/  types/
runner/                browser runner: supervisor, image definition and its tests
fixtures/demo-app/     small web application used as a target in browser tests
backend/evaluation/    labelled datasets and the evaluation harness (`make evaluate`)
backend/ci/            one-file client for running tests from a pipeline
e2e/                   browser walk-through of the web application (`make e2e`)
docs/                  guides, design documents and progress
scripts/               development scripts
docker-compose.yml       development and test stack
docker-compose.prod.yml  production stack (see docs/deployment.md)
.env.example             development settings template
.env.production.example  production settings template
```

## Conventions

- **Errors**: every failing API response uses one envelope
  (`application/problem+json` with `code`, `title`, `detail`, `errors[]`,
  `request_id`). Services raise exceptions from `app.core.errors`; they never
  build HTTP responses.
- **Request IDs**: every response carries `X-Request-ID`. Send your own (8–64
  characters of `A–Z a–z 0–9 . _ -`) to correlate with a caller; it appears in
  every log line for that request.
- **Logs**: one JSON object per line on stdout, from the API and the worker.
- **API versioning**: product endpoints live under `/api/v1`. `/healthz` and
  `/readyz` are unversioned.
- **Transactions**: one per request. It commits when the handler returns and
  rolls back when it raises. Repositories flush but never commit.
- **Tenant data**: read and write it through a `TenantRepository`, inside a
  request that depends on `OrgContextDep`. New tenant tables use `TenantMixin`
  and must be added to the row-level security list in their migration; a test
  fails if one is missed.
- **Authorization**: check a `Permission` (`ctx.require(...)`), never a role name.
- **Outbound requests to user-supplied URLs**: always through
  `AccessValidator.validate` for every hop, connecting to the address it returns.
  Never fetch a user's URL directly.
- **Secrets**: accepted on input only. Response models are built field by field
  and have no field that could carry one; do not add one.
- **LLM calls**: only through `LLMGateway` (`app/agent/llm`), never a provider SDK
  directly. Output is a Pydantic model with `extra="forbid"`; size limits go in
  validators, not in the JSON schema (strict mode rejects length keywords).
- **Prompts**: files in `app/agent/prompts/` named `<name>.v<n>.md`. To change
  wording, add a new version; do not edit a version that has produced results.
- **Untrusted text in prompts**: clean it with `clean_text` and wrap it with a
  `Fence`. The system message stays fixed. Never put a secret in a prompt.
- **Readiness and verdicts are code, not model output**: the model proposes,
  `validate_requirement` / `validate_case` decide, a person approves.
- **Expected results are never guessed**: a result is either known with a source,
  or `unknown` and empty. This holds in the schemas, the database and the prompts;
  keep it that way in anything new.
- **Test case changes are new versions**: never update a `test_case_versions` row
  in place; call `write_version`.
- **Browser actions**: only through `BrowserSession.call`, in a session opened
  with `BrowserSessionManager.open`. Never call the MCP connection directly: the
  session is where permissions, address checks, redaction and recording happen.
- **A failed browser action raises.** Do not catch a `BrowserError` and carry on
  as if the step ran, and do not derive "passed" from the absence of an error
  message. Assign a pass in one place, after the work completed.
- **New MCP tools are off until classified.** To use one, add a rule in
  `app/browser/policy.py` with the narrowest category and argument list that
  works, and run `make be-test-browser`.
- **Secrets in a browser session**: register them with `session.add_secrets`
  before the first call that uses them.
- **Test steps**: everything the engine can do is a step type in
  `app/testing/automation.py` with a strict schema. To add a capability, add a
  step type, its handler and tests in `tests/engine`; never a step that takes
  code or a raw selector.
- **Failed versus error**: raise `StepFailure` when a check ran and did not hold,
  `StepError` when a step could not be carried out. Do not turn one into the other.
- **Verdicts come from `app/testing/verdicts.py`** and from step outcomes, never
  from a model and never from the absence of an error.
- **Engine and Playwright versions move together**: the package in
  `pyproject.toml` and the image in `backend/Dockerfile` (target `engine`).
- **Evidence**: storage keys are generated by `EvidenceStore`; never build a path
  from request input.
- **Workflow nodes** return updates to the state and never raise for an expected
  problem: raise `WorkflowError` (or let a service's error through) and the run
  ends as failed with a report. Keep every node safe to run twice, because a
  recovered run starts its interrupted node again.
- **Workflow state holds IDs and short summaries only.** It is stored in
  checkpoints: never put a secret, page content or a script in it.
- **Conditional edges are pure functions** in `app/workflow/routing.py`. A model's
  output never selects an edge, a tool or a status.
- **A new model call** needs a strict output schema (`extra="forbid"`, every field
  required), a versioned prompt that fences untrusted input, validation of the
  answer by code, and a person's approval before the answer has any effect.
- **Frontend**: the access token stays in memory (Pinia), never in
  `localStorage`. Hiding a button is a convenience; the API enforces every rule.
