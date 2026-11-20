# syntax=docker/dockerfile:1

# ---- base: runtime dependencies only -----------------------------------------
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/opt/venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

ARG APP_UID=1000
RUN useradd --uid "${APP_UID}" --create-home --shell /usr/sbin/nologin app

# Where the worker stores evidence. Created here so that the volume mounted over it
# starts out owned by the application user.
RUN mkdir -p /var/lib/qa-agent/evidence && chown app:app /var/lib/qa-agent/evidence

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---- dev: adds linters, type checker and test tools --------------------------
FROM base AS dev
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen
# Source is bind-mounted by Docker Compose; copied here so the image also works alone.
COPY --chown=app:app . .
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "app"]

# ---- prod: application code, no dev tooling ----------------------------------
FROM base AS prod
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app app ./app
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

# ---- engine: the test engine, on the official Playwright image (browsers included) ----
# The image version and the "playwright" version in pyproject.toml must be the same:
# the Python package only drives the browser builds it was released with.
FROM mcr.microsoft.com/playwright/python:v1.65.2-noble AS engine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
# Includes the test tools: the engine's own tests need a browser and so run in this image.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen
COPY --chown=pwuser:pwuser . .
USER pwuser
EXPOSE 8902
CMD ["uvicorn", "app.engine.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8902"]

# ---- engine-prod: the test engine without test tools or test code ----------------------
FROM mcr.microsoft.com/playwright/python:v1.65.2-noble AS engine-prod

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev
COPY --chown=pwuser:pwuser app ./app
USER pwuser
EXPOSE 8902
CMD ["uvicorn", "app.engine.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8902"]
