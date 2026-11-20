# Installation and Environment Setup

How to get the platform running on a development machine. For production see
[deployment.md](deployment.md).

## 1. Requirements

| | |
|---|---|
| Operating system | Linux, macOS or Windows with WSL 2 |
| Docker | Engine 24+ with Compose v2 (`docker compose version`) |
| `make` | any |
| Memory | 8 GB free for the whole stack with browsers |
| Disk | about 8 GB for images |

Python 3.12 and Node 22 run in containers; nothing else is installed on the host.

## 2. Install

```sh
git clone <repository> && cd <repository>
make up
```

`make up` does four things:

1. creates `.env` from `.env.example` and generates every secret in it
   (`scripts/init-env.sh`; safe to re-run, existing values are kept);
2. builds the images. The first build downloads the Playwright MCP image
   (about 1.7 GB) and the Playwright browsers image (about 3.5 GB);
3. applies the database migrations;
4. starts every service and waits until each one is healthy.

Then open:

| What | Address |
|---|---|
| Web application | http://localhost:5173 |
| API documentation (development only) | http://localhost:8000/api/docs |
| Liveness / readiness | http://localhost:8000/healthz · http://localhost:8000/readyz |

Register an account: the first registration creates your organization and makes
you its owner.

If a port is taken, change the `*_HOST_PORT` values in `.env` and run `make up` again.

## 3. Environment variables

All configuration is environment variables; `.env.example` documents each one.
The groups:

| Group | Variables | Needed? |
|---|---|---|
| Database | `POSTGRES_*`, `APP_DB_USER`, `APP_DB_PASSWORD` | generated |
| Signing and encryption | `JWT_SECRET_KEY`, `CREDENTIAL_MASTER_KEYS` | generated; the process refuses to start without them |
| Internal services | `RUNNER_TOKEN`, `EGRESS_ADMIN_TOKEN`, `ENGINE_TOKEN` | generated |
| AI service | `LLM_PROVIDER`, `OPENAI_API_KEY` or `GEMINI_API_KEY`, `LLM_REQUESTS_PER_MINUTE`, `LLM_MODEL_ANALYSIS`, `LLM_MODEL_GENERATION`, prices and budget | optional; without a key the AI features answer 503 and everything else works |
| Mail | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM` | optional; without a host, emails are written to the API log in development |
| Metrics | `METRICS_TOKEN` | optional; without it `/metrics` does not exist |
| GitHub | `GITHUB_API_URL` | only for GitHub Enterprise Server |
| Host ports | `API_HOST_PORT`, `FRONTEND_HOST_PORT`, `POSTGRES_HOST_PORT`, `REDIS_HOST_PORT` | change on conflict |

After changing `.env`, run `make up` again; containers pick up the new values.

### Switching the AI features on

```
LLM_PROVIDER=openai            # or: gemini
OPENAI_API_KEY=...             # or: GEMINI_API_KEY=...
LLM_REQUESTS_PER_MINUTE=0      # on a rate-limited key, for example 6 for a 15-per-minute quota
LLM_MODEL_ANALYSIS=<a model your account can use>
LLM_MODEL_GENERATION=<optional; defaults to the analysis model>
```

There is deliberately no default model. Set the two price variables as well if
you want cost tracking and the monthly budget cap.

### Testing an application on your own machine

Addresses on localhost and private networks are blocked by default. The README
section "Testing applications on localhost or a private network" describes the
administrator-controlled setting that permits specific ones.

## 4. Try it without your own application

A small demo application is included as a target, and one command fills the
stack with something to look at:

```sh
make demo
```

It creates a login (`demo@example.com`; the generated password is the
`DEMO_PASSWORD` line in `.env`), a project "Demo shop" pointed at the demo
application, a test login for it, three approved test cases in a suite (one is
meant to fail: it runs against a page with a seeded defect) and one finished run
with a report. Run again, it changes nothing.

Inside the stack it answers at `http://demo-app:8080` (login `demo` / `demo-password`, a fixture value, not a secret). The automated walk-through uses it:

```sh
make e2e          # drives the web application in a real browser, start to report
```

## 5. Everyday commands

```sh
make ps                 # status
make logs s=worker      # follow one service
make down               # stop; data is kept
make clean              # stop and DELETE the data volumes
make check              # every lint, type check and unit test
make help               # everything else
```

Running the tests is described in [testing-guide.md](testing-guide.md).

## 6. Troubleshooting

| Symptom | Cause and remedy |
|---|---|
| `make up` waits, then reports a service unhealthy | `make logs s=<service>`. Most often a port conflict or too little memory for the browser images |
| The API exits at once with a settings error | A required variable is empty or malformed; the message names it. Delete the line from `.env` and run `make env` to regenerate it |
| AI features answer 503 `llm_not_configured` | No key for the chosen `LLM_PROVIDER`, or no `LLM_MODEL_ANALYSIS` |
| A run ends as `error` with a network category | The application's address is not in the project's allowlist, or is private and not permitted |
| No email arrives | No `SMTP_HOST`: look for `email_not_delivered_dev_mode` in `make logs s=api` and copy the link |
| Files created by containers belong to another user | Set `APP_UID` in `.env` to `id -u`, then `make build` |
