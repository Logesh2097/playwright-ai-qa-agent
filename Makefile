# Developer commands. Everything runs in Docker, so the only host requirements
# are Docker (with Compose v2) and make.

COMPOSE := docker compose
BACKEND := $(COMPOSE) run --rm --no-deps api
FRONTEND := $(COMPOSE) run --rm --no-deps frontend

.DEFAULT_GOAL := help
.PHONY: demo e2e help env build up down clean ps logs migrate migration \
        be-lint be-format be-format-check be-typecheck be-test be-test-integration be-test-browser engine-test runner-test be-check be-lock be-shell \
        fe-lint fe-format fe-format-check fe-typecheck fe-test fe-build fe-check fe-shell check

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# --- Stack ---------------------------------------------------------------------
env: ## Create .env from the template (generates a DB password)
	@./scripts/init-env.sh

build: ## Build all images
	$(COMPOSE) build

up: env ## Start the full stack in the background and wait until healthy
	$(COMPOSE) up -d --build --wait

down: ## Stop the stack (data volumes are kept)
	$(COMPOSE) down

clean: ## Stop the stack and DELETE its data volumes
	$(COMPOSE) down --volumes

ps: ## Show service status
	$(COMPOSE) ps

logs: ## Follow logs (make logs s=api)
	$(COMPOSE) logs -f $(s)

# --- Database ------------------------------------------------------------------
migrate: ## Apply database migrations
	$(COMPOSE) run --rm migrate

migration: ## Create a migration (make migration m="add users")
	$(COMPOSE) run --rm migrate sh -c 'python -m app.db.bootstrap && alembic revision --autogenerate -m "$(m)"'

# --- Backend -------------------------------------------------------------------
be-lint: ## Ruff lint
	$(BACKEND) ruff check .

be-format: ## Ruff format (rewrites files)
	$(BACKEND) sh -c "ruff check --fix . ; ruff format ."

be-format-check: ## Ruff format check
	$(BACKEND) ruff format --check .

be-typecheck: ## mypy (strict)
	$(BACKEND) mypy

be-test: ## Backend unit tests
	$(BACKEND) pytest

be-test-integration: ## All backend tests against a scratch database, Redis and the worker (stack must be up)
	$(COMPOSE) run --rm -e RUN_INTEGRATION=1 migrate pytest

be-test-browser: ## Browser tests with a real Playwright MCP server, browser and demo app (stack must be up)
	$(COMPOSE) --profile test up -d demo-app
	$(COMPOSE) --profile test run --rm browser-tests

engine-test: ## Engine tests: every action, locator and assertion in real browsers against the demo app
	$(COMPOSE) --profile test up -d demo-app
	$(COMPOSE) --profile test run --rm engine-tests

demo: ## Create a demo login, project, tests and one finished run (stack must be up)
	@./scripts/demo.sh

e2e: ## Walk through the web application in a real browser (stack and demo app must be running)
	$(COMPOSE) --profile test up -d demo-app
	docker run --rm --network host -u "$$(id -u):$$(id -g)" -e HOME=/tmp -e E2E_OUT=/e2e/output \
		-v "$(CURDIR)/e2e":/e2e -w /e2e qa-agent-engine:dev python walkthrough.py

evaluate: ## Measure the rule-based components on labelled data, and the model-based ones if a model is configured
	docker run --rm -u "$$(id -u):$$(id -g)" -e HOME=/tmp --env-file .env \
		-v "$(CURDIR)/backend":/app -w /app qa-agent-engine:dev python -m evaluation.run --out evaluation/results

runner-test: ## Tests of the browser runner supervisor (no browser needed)
	docker run --rm -v "$(CURDIR)/runner":/src:ro -w /src --entrypoint python3 qa-agent-runner:dev -m unittest -q test_supervisor

be-check: be-lint be-format-check be-typecheck be-test ## All backend checks

be-lock: ## Refresh backend/uv.lock after editing pyproject.toml
	docker run --rm -u "$$(id -u):$$(id -g)" -e HOME=/tmp -v "$(CURDIR)/backend":/app -w /app \
		ghcr.io/astral-sh/uv:0.9-python3.12-bookworm-slim uv lock

be-shell: ## Shell in a backend container
	$(BACKEND) bash

# --- Frontend ------------------------------------------------------------------
fe-lint: ## ESLint
	$(FRONTEND) npm run lint

fe-format: ## Prettier (rewrites files)
	$(FRONTEND) npm run format

fe-format-check: ## Prettier check
	$(FRONTEND) npm run format:check

fe-typecheck: ## vue-tsc
	$(FRONTEND) npm run typecheck

fe-test: ## Vitest
	$(FRONTEND) npm test

fe-build: ## Production build
	$(FRONTEND) npm run build

fe-check: fe-lint fe-format-check fe-typecheck fe-test ## All frontend checks

fe-shell: ## Shell in a frontend container
	$(FRONTEND) sh

# --- Everything ----------------------------------------------------------------
check: be-check fe-check ## Run every lint, type check and unit test
