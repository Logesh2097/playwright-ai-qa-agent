# Deployment Guide

How to run the platform in production. For a development machine see
[installation.md](installation.md).

**Status.** The production configuration below was rehearsed on a single host on
2026-11-20: the production images were built, the stack started healthy, and the
full browser walk-through (173 checks) passed against it. The rehearsal stack was
then removed. **The platform has not been deployed to any production environment**,
and nothing in this repository deploys automatically: the CI workflow builds and
checks, and stops there. A deployment is a deliberate act by an operator.

## 1. Three separate configurations

| | Development | Test | Production |
|---|---|---|---|
| Compose file | `docker-compose.yml` | `docker-compose.yml` with `--profile test` | `docker-compose.prod.yml` |
| Compose project | `qa-agent` | `qa-agent` | `qa-agent-prod` |
| Settings file | `.env` (from `.env.example`, by `make env`) | `.env`; tests use a scratch database and a Redis database of their own | `.env.production` (from `.env.production.example`) |
| `APP_ENV` | `development` | `test` | `production` |
| Images | `dev` targets, source mounted, reload | same, plus the demo application | `prod` targets, no source mounted, no dev dependencies |
| Published ports | API, frontend, PostgreSQL, Redis | same | the web server only |
| API docs (`/api/docs`) | on | on | off (404) |
| Emails | written to the log unless SMTP is set | captured in memory | sent through SMTP |
| Refresh cookie | `Secure` may be off for local HTTP | — | `Secure` required; the process refuses to start otherwise |
| HSTS | off | off | on |

The two compose projects have different names, networks and volumes, so they can
exist on one machine without touching each other. Nothing is shared between them
except the source tree the images are built from.

## 2. What runs

```
            TLS proxy / load balancer   (yours; not part of the stack)
                       |
                    web (nginx)  ───────────── edge ──── api (FastAPI, uvicorn)
                                                           |
                                              backend ─────┼──── postgres, redis
                                                           |
                                                        worker (arq)
                                                           |
                                   runner network (internal, no route out)
                                   ├── runner  (Playwright MCP sessions)
                                   ├── engine  (Playwright test runs)
                                   └── egress-proxy ──── egress ──── applications under test
```

- `web` serves the built Vue application and proxies `/api/`, `/healthz` and
  `/readyz` to `api`. It is the only service with a published port
  (`WEB_BIND:WEB_PORT`, loopback by default). `/metrics` is deliberately not proxied.
- `migrate` runs once per `up`, as the schema owner; `api` and `worker` wait for it.
- Browsers (`runner`, `engine`) sit on an internal network. Their only way out is
  `egress-proxy`, which enforces each project's address allowlist and the SSRF rules.
- Every application container runs as a non-root user with a read-only root
  filesystem, all capabilities dropped and `no-new-privileges`.

## 3. Prerequisites

- A Linux host with Docker Engine 24+ and Compose v2.
- Memory: the defaults in `.env.production.example` add up to roughly 10 GB of
  limits (engine 4 GB, runner 3 GB, API and worker 1 GB each, plus the data
  stores). Disk: about 7 GB of images, plus the database and evidence.
- A DNS name and a TLS certificate, and something to terminate TLS (§6).
- An SMTP account, if invitations and password resets are to work.
- An OpenAI or Gemini API key (`LLM_PROVIDER`) and model names, if the AI features are to work. Without
  them the platform runs and those features answer 503.

## 4. Secrets

Nothing secret is in an image, a Dockerfile or the source tree. Every secret
arrives as an environment variable, and the compose file refuses to start while
a required one is empty (`docker compose config` fails and names the variable).

| Variable | What it protects | How to generate |
|---|---|---|
| `JWT_SECRET_KEY` | sessions, evidence download addresses | `openssl rand -base64 48 \| tr -dc A-Za-z0-9 \| head -c 48` |
| `CREDENTIAL_MASTER_KEYS` | stored test credentials, GitHub tokens | `echo "1:$(openssl rand -base64 32)"` |
| `POSTGRES_PASSWORD`, `APP_DB_PASSWORD` | the database (owner role; row-level-security role) | as the first |
| `RUNNER_TOKEN`, `EGRESS_ADMIN_TOKEN`, `ENGINE_TOKEN` | calls between internal services | as the first |
| `METRICS_TOKEN` (optional) | `GET /metrics` | as the first |
| `SMTP_PASSWORD`, `OPENAI_API_KEY` or `GEMINI_API_KEY` | external accounts | from the provider |

Rules:

- `.env.production` is ignored by git. Keep it readable by the deploying user
  only (`chmod 600`), or better, do not keep it on disk: have a secret manager
  put the variables into the environment of the `docker compose` command.
- **Back up `CREDENTIAL_MASTER_KEYS` separately from the database.** Without it
  the stored credentials cannot be decrypted; with the database and the key
  together anyone can.
- Use different values in every environment. Never copy `.env` to production.
- Rotation is described in [operations.md](operations.md) §6.

## 5. First deployment

```sh
git clone <repository> && cd <repository>
cp .env.production.example .env.production      # fill in every empty value
chmod 600 .env.production

alias dc='docker compose -f docker-compose.prod.yml --env-file .env.production'
dc config --quiet            # fails here if a required value is missing
dc build
dc up -d --wait              # migrations run first; waits until every service is healthy
dc ps
```

Check it before sending traffic:

```sh
curl -fsS http://127.0.0.1:8080/healthz      # the API process answers
curl -fsS http://127.0.0.1:8080/readyz       # database and Redis reachable
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/api/docs   # 404
```

The first person to register creates the first organization and owns it. Then,
signed in as an administrator, the Settings page (`GET /api/v1/system/services`)
shows whether the test engine and the browser runner answer.

A started stack is not a verified one. Before relying on it, run one real test
end to end: register an application under test, run a small approved suite, and
open its report and evidence.

## 6. TLS and the address in front

The stack speaks plain HTTP on `WEB_BIND:WEB_PORT` and expects a proxy that
terminates TLS in front of it. That proxy must:

- forward `Host`, `X-Forwarded-For` and `X-Forwarded-Proto`;
- not buffer `GET /api/v1/…/events` (live progress is a server-sent event
  stream) and let it stay open for 30 minutes;
- allow request bodies up to 5 MB.

Set `PUBLIC_BASE_URL` to the public `https://` address: links in emails, CI
answers and GitHub statuses are built from it. Set `FORWARDED_ALLOW_IPS` to the
proxy's address rather than `*` when the proxy is on another host; rate limits
and audit records use the forwarded client address.

## 7. Database migrations

- `migrate` runs `alembic upgrade head` on every `dc up`, before the API and the
  worker start. It connects as the owner role; the application role cannot
  change the schema and is subject to row-level security.
- Treat migrations as forward-only. Take a backup (§9) before any upgrade that
  includes one. Downgrades exist for each revision but are exercised only in
  development.
- To see what a release would do without applying it:
  `dc run --rm migrate alembic upgrade head --sql`.
- CI runs `alembic check`, so a model change without a migration fails the build.

## 8. Health, logs and monitoring

| What | Where |
|---|---|
| Liveness | `GET /healthz` |
| Readiness (database, Redis) | `GET /readyz` |
| Engine and browser runner state | `GET /api/v1/system/services`, refreshed every minute by the worker |
| Container health | `dc ps`; every service has a health check and `restart: unless-stopped` |
| Metrics | `GET /metrics` on the `api` container, Prometheus format, only with `METRICS_TOKEN` set, reachable only from inside the Docker networks |
| Logs | JSON, one object per line, on stdout: `dc logs -f api worker`. Rotated by Docker at 5 × 20 MB per container |

Logs carry a request ID and never contain passwords, tokens, credential values
or email bodies. To keep them longer than the rotation allows, point a log
shipper at the Docker JSON files or change the logging driver.

To scrape metrics, run the scraper as a container attached to the `edge`
network and give it the token as a bearer token. Metrics and useful alerts are
listed in [operations.md](operations.md) §2.

## 9. Backup and restore

Three things hold state:

| What | Volume | Needed to restore? |
|---|---|---|
| PostgreSQL | `qa-agent-prod_pgdata` | yes |
| Evidence files | `qa-agent-prod_evidence` | yes, for the screenshots, videos, traces and logs of past runs |
| Redis | `qa-agent-prod_redisdata` | no: queued jobs and rate-limit counters. An execution found running without a worker is closed as an error by the recovery job |

And one thing outside the volumes: `CREDENTIAL_MASTER_KEYS`.

Back up the database and the evidence at the same time, because results refer
to evidence files:

```sh
dc exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > qa-agent-$(date +%F).dump
docker run --rm -v qa-agent-prod_evidence:/data:ro -v "$PWD":/backup alpine \
    tar czf /backup/evidence-$(date +%F).tar.gz -C /data .
```

Restore, onto an empty stack:

```sh
dc up -d --wait postgres
dc run --rm migrate python -m app.db.bootstrap          # creates the application role
dc exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' < qa-agent-DATE.dump
docker run --rm -v qa-agent-prod_evidence:/data -v "$PWD":/backup alpine \
    tar xzf /backup/evidence-DATE.tar.gz -C /data
dc up -d --wait
```

**These commands have not been rehearsed against the production stack.** Run a
restore into a scratch environment before depending on the backups, and repeat
it on a schedule.

## 10. Upgrades and rollback

```sh
git pull                                  # or check out the release tag
# set IMAGE_TAG in .env.production to the release, so the previous images stay available
dc build
# back up (§9)
dc up -d --wait
```

`dc up` recreates changed containers. The worker is given 60 seconds to finish
its current job; an execution still running after that is closed as an error by
the recovery job and can be run again. There is no zero-downtime upgrade on a
single host: expect the API to be away for some seconds.

Rollback: set `IMAGE_TAG` back and `dc up -d --wait`. If the release included a
migration, restore the backup taken before it rather than downgrading.

## 11. Cloud deployment (GCP-compatible layout)

The stack does not depend on a cloud provider. On Google Cloud the same pieces
map as follows. **This layout is a design; it has not been run.**

| Piece | On one Compute Engine VM | Managed alternative |
|---|---|---|
| The compose stack | VM with Docker, `docker-compose.prod.yml` as is | — |
| TLS and public address | External HTTPS load balancer with a managed certificate; backend = the VM on `WEB_PORT` (bind `WEB_BIND` to the VM's private address) | same |
| PostgreSQL | `postgres` container on a persistent disk, disk snapshots | Cloud SQL for PostgreSQL 16: drop the `postgres` service and point `DATABASE_URL` and `MIGRATION_DATABASE_URL` at it |
| Redis | `redis` container | Memorystore for Redis, with the `noeviction` policy |
| Evidence | `evidence` volume on a persistent disk | a Filestore share mounted into `api` and `worker`. Cloud Storage would need a new `EvidenceStorage` implementation: the interface exists, that implementation does not |
| Secrets | `.env.production`, mode 600 | Secret Manager, read by the deploy script into the environment |
| Images | built on the VM | Artifact Registry: set `IMAGE_PREFIX` to the registry path, build and push from a release pipeline you authorise, `dc pull` on the VM |
| Logs and metrics | Docker logs | Ops Agent for logs; Managed Service for Prometheus scraping `/metrics` |

Constraints to keep in any layout:

- `runner`, `engine` and `egress-proxy` must stay on a network with no route
  out except through the proxy. On Kubernetes (GKE) that means a NetworkPolicy
  of the same shape; the browsers must never get a route to the internet or to
  the metadata server.
- The API and the worker must see the same evidence storage.
- Cloud Run does not fit the runner and the engine: they need long-lived
  browser processes, shared memory and an isolated network.

## 12. Deployment checklist

- [ ] `.env.production` filled in; no value shared with another environment
- [ ] `CREDENTIAL_MASTER_KEYS` stored separately from the database backups
- [ ] `dc config --quiet` passes
- [ ] TLS proxy in place; `PUBLIC_BASE_URL` is the https address
- [ ] `WEB_BIND` is loopback or a private address; no other port is published
- [ ] All services healthy; `/readyz` answers; `/api/docs` is 404
- [ ] Settings page shows the engine and the browser runner available
- [ ] A test invitation email arrives
- [ ] One real suite run end to end; its report and evidence open
- [ ] Backup taken and a restore rehearsed
- [ ] Metrics scraped and alerts set, or a decision recorded not to
- [ ] [known-limitations.md](known-limitations.md) read and accepted
