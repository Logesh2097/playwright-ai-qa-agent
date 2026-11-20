"""Shared fixtures.

Unit tests never open a network connection: the values below are placeholders
that satisfy settings validation.

Integration tests (RUN_INTEGRATION=1) run against a scratch database,
`<name>_test`, that is dropped, recreated and migrated with the real Alembic
migrations once per session, and against Redis database 15. The development
database is never touched. They need both DATABASE_URL (application role) and
MIGRATION_DATABASE_URL (schema owner) in the environment; `make
be-test-integration` provides them.
"""

import asyncio
import base64
import os
import re
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic.config import Config
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://unit:unit@localhost:1/unit")
os.environ.setdefault("REDIS_URL", "redis://localhost:1/0")
os.environ.setdefault("JWT_SECRET_KEY", "unit-test-signing-key-0123456789abcdef")
# base64 of 32 bytes; a fixed, public test key
TEST_MASTER_KEYS = "1:" + base64.b64encode(b"unit-test-master-key-32-bytes!!!").decode()
os.environ.setdefault("CREDENTIAL_MASTER_KEYS", TEST_MASTER_KEYS)

from app.agent.llm.base import LLMCallRecord, LLMError, LLMRequest, LLMResult, LLMUsage
from app.api.deps import get_health_service
from app.core.config import Settings
from app.core.security.url_safety import AccessValidator
from app.main import create_app
from app.models import Base
from app.services.email import EmailMessage
from app.services.health import HealthService
from app.worker import jobs as worker_jobs
from tests.factories import analysis_dict, generated

BACKEND_ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "correct horse battery staple"


async def _ok() -> None:
    return None


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        "database_url": "postgresql+asyncpg://unit:unit@localhost:1/unit",
        "redis_url": "redis://localhost:1/0",
        "jwt_secret_key": "unit-test-signing-key-0123456789abcdef",
        # Explicit, so a developer's local AUTH_COOKIE_SECURE=false cannot leak into tests.
        "auth_cookie_secure": True,
        "credential_master_keys": TEST_MASTER_KEYS,
        # No AI service by default; tests that need one configure it or use FakeGateway.
        "llm_provider": "openai",
        "openai_api_key": None,
        "gemini_api_key": None,
        "llm_requests_per_minute": 0,
        "llm_model_analysis": None,
        "openai_base_url": None,
        "llm_input_price_per_mtok": None,
        "llm_output_price_per_mtok": None,
        "llm_monthly_budget_usd": None,
        # No browser services by default, whatever the container's environment says.
        "runner_url": None,
        "runner_token": None,
        "egress_admin_url": None,
        "egress_admin_token": None,
        "egress_proxy_url": None,
        "engine_url": None,
        "engine_token": None,
        "evidence_root": tempfile.gettempdir() + "/qa-agent-test-evidence",
    }
    values.update(overrides)
    return Settings.model_validate(values)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    """Application without lifespan; readiness dependencies are faked as healthy."""
    application = create_app(settings)
    application.dependency_overrides[get_health_service] = lambda: HealthService(
        {"postgres": _ok, "redis": _ok}, timeout_s=1.0
    )
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    gates = (
        ("integration", "RUN_INTEGRATION", "integration test; set RUN_INTEGRATION=1"),
        ("browser", "RUN_BROWSER", "needs a real browser; run `make be-test-browser`"),
        ("engine", "RUN_ENGINE", "needs the engine image; run `make engine-test`"),
    )
    for marker, variable, reason in gates:
        if os.environ.get(variable) == "1":
            continue
        for item in items:
            if marker in item.keywords:
                item.add_marker(pytest.mark.skip(reason=reason))


# --- integration: scratch database ------------------------------------------------


@dataclass(frozen=True)
class TestDatabase:
    __test__ = False

    app_url: str  # application role: subject to row-level security
    owner_url: str  # schema owner: bypasses row-level security
    redis_url: str


def alembic_config(owner_url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.attributes["database_url"] = owner_url
    return config


async def _recreate_database(admin_url: str, name: str, app_role: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    async with engine.connect() as connection:
        await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
        await connection.execute(text(f'GRANT CONNECT ON DATABASE "{name}" TO "{app_role}"'))
    await engine.dispose()


@pytest.fixture(scope="session")
def test_database() -> TestDatabase:
    migration_url = os.environ.get("MIGRATION_DATABASE_URL")
    if not migration_url:
        pytest.fail("integration tests need MIGRATION_DATABASE_URL (use make be-test-integration)")
    app = make_url(os.environ["DATABASE_URL"])
    owner = make_url(migration_url)
    name = f"{owner.database}_test"
    assert app.username and app.username != owner.username, "two distinct database roles needed"

    asyncio.run(_recreate_database(migration_url, name, app.username))
    owner_url = owner.set(database=name).render_as_string(hide_password=False)
    command.upgrade(alembic_config(owner_url), "head")

    redis = make_url(os.environ["REDIS_URL"]).set(database="15")
    return TestDatabase(
        app_url=app.set(database=name).render_as_string(hide_password=False),
        owner_url=owner_url,
        redis_url=redis.render_as_string(hide_password=False),
    )


@pytest.fixture
async def owner_engine(test_database: TestDatabase) -> AsyncIterator[AsyncEngine]:
    """Connects as the schema owner. For test setup and assertions that must see everything."""
    engine = create_async_engine(test_database.owner_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
async def app_engine(test_database: TestDatabase) -> AsyncIterator[AsyncEngine]:
    """Connects as the application role, exactly like the API does."""
    engine = create_async_engine(test_database.app_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
async def clean_state(test_database: TestDatabase, owner_engine: AsyncEngine) -> None:
    """Empty every table and the test Redis database."""
    tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    async with owner_engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    redis = Redis.from_url(test_database.redis_url)
    await redis.flushdb()
    await redis.aclose()


@pytest.fixture
def integration_settings(test_database: TestDatabase) -> Settings:
    return make_settings(
        database_url=test_database.app_url,
        redis_url=test_database.redis_url,
        login_max_failures=3,
        register_ip_limit=1000,
        login_ip_limit=1000,
        password_reset_limit=3,
        public_base_url="https://qa.example",
    )


@dataclass
class Outbox:
    """Stands in for the mail transport and keeps what would have been sent."""

    messages: list[EmailMessage] = field(default_factory=list)

    async def send(self, message: EmailMessage) -> None:
        self.messages.append(message)

    def token_for(self, email: str) -> str:
        message = next(m for m in reversed(self.messages) if m.to == email)
        match = re.search(r"token=([\w-]+)", message.body)
        assert match, "no token link in the email"
        return match.group(1)


PUBLIC_IP = "93.184.216.34"


@dataclass
class FakeNetwork:
    """Stands in for DNS and for the applications under test. Nothing leaves the process."""

    hosts: dict[str, list[str]] = field(
        default_factory=lambda: {
            "acme.io": [PUBLIC_IP],
            "staging.acme.io": [PUBLIC_IP],
            "api.staging.acme.io": [PUBLIC_IP],
            "auth.acme.io": [PUBLIC_IP],
            "prod.acme.io": [PUBLIC_IP],
            "evil.example": ["203.0.113.66"],
            "internal.acme.io": ["10.0.0.5"],
            "meta.acme.io": ["169.254.169.254"],
            "mixed.acme.io": [PUBLIC_IP, "127.0.0.1"],
            "localhost": ["127.0.0.1"],
        }
    )
    # "https://host/path" -> (status, headers)
    routes: dict[str, tuple[int, dict[str, str]]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    lookups: list[str] = field(default_factory=list)

    async def resolve(self, host: str, port: int) -> list[str]:
        self.lookups.append(host)
        return list(self.hosts.get(host, []))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = f"{request.url.scheme}://{request.headers['host']}{request.url.path}"
        status, headers = self.routes.get(key, (200, {}))
        return httpx.Response(status, headers=headers, text="<html>secret page body</html>")


@dataclass
class FakeGateway:
    """A scripted model. Each analysis consumes the next item: a dict to return as the
    analysis, or an exception to raise. Records every request it is given."""

    script: list[Any] = field(default_factory=list)
    requests: list[LLMRequest[Any]] = field(default_factory=list)
    configured: bool = True

    async def structured(self, request: LLMRequest[Any]) -> LLMResult[Any]:
        self.requests.append(request)
        if self.script:
            step = self.script.pop(0)
        elif request.purpose == "generation":
            step = {"cases": [generated()]}
        else:
            step = analysis_dict()
        usage = LLMUsage(input_tokens=1200, output_tokens=300, cost_usd=Decimal("0.0100"))
        status = step.code if isinstance(step, LLMError) else "ok"
        if request.recorder is not None:
            await request.recorder(
                LLMCallRecord(
                    purpose=request.purpose,
                    model="fake-model",
                    prompt=request.prompt,
                    status=status,
                    usage=LLMUsage() if isinstance(step, Exception) else usage,
                    latency_ms=5,
                    attempt=1,
                )
            )
        if isinstance(step, Exception):
            raise step
        output = request.schema.model_validate(step)
        return LLMResult(output=output, model="fake-model", usage=usage, calls=1, latency_ms=5)


@dataclass
class InlineQueue:
    """Collects jobs instead of sending them to Redis; tests run them when they choose."""

    pending: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def enqueue(self, job: str, **kwargs: Any) -> None:
        self.pending.append((job, kwargs))


@dataclass
class Api:
    """A running application plus helpers for the flows most tests start with."""

    app: FastAPI
    outbox: Outbox
    network: FakeNetwork = field(default_factory=FakeNetwork)
    llm: FakeGateway = field(default_factory=FakeGateway)
    queue: InlineQueue = field(default_factory=InlineQueue)
    # What the worker would use to start browsers; tests install a fake or the real one.
    browser_manager: Any = None
    # The worker's test engine and egress clients; tests install fakes or the real ones.
    engine: Any = None
    egress: Any = None

    async def run_jobs(self, gateway: Any = None) -> list[str]:
        """Run every queued job the way the worker would. Returns their results."""
        results = []
        while self.queue.pending:
            name, kwargs = self.queue.pending.pop(0)
            context = {
                "session_factory": self.app.state.session_factory,
                "llm_gateway": gateway or self.llm,
                "settings": self.app.state.settings,
                "app_redis": self.app.state.redis,
                "credential_cipher": self.app.state.credential_cipher,
                "browser_manager": self.browser_manager,
                "engine_client": self.engine,
                "egress_client": self.egress,
                "job_queue": self.queue,
                "github_transport": getattr(self.app.state, "github_transport", None),
            }
            results.append(await getattr(worker_jobs, name)(context, **kwargs))
        return results

    def client(self) -> httpx.AsyncClient:
        """A new client with its own cookie jar, i.e. a separate browser."""
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="https://test",
            headers={"X-Requested-With": "pytest"},
        )

    async def register(
        self, email: str, organization: str | None = "Acme", **extra: Any
    ) -> "Account":
        client = self.client()
        body: dict[str, Any] = {"email": email, "password": PASSWORD, "full_name": "Test Person"}
        if organization is not None:
            body["organization_name"] = organization
        body.update(extra)
        response = await client.post("/api/v1/auth/register", json=body)
        assert response.status_code == 201, response.text
        return Account(client=client, email=email, session=response.json())

    async def login(self, email: str, password: str = PASSWORD) -> "Account":
        client = self.client()
        response = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": password}
        )
        assert response.status_code == 200, response.text
        return Account(client=client, email=email, session=response.json())

    async def invite_and_join(self, inviter: "Account", email: str, role: str) -> "Account":
        response = await inviter.post(
            f"/api/v1/organizations/{inviter.org_id}/invitations",
            json={"email": email, "role": role},
        )
        assert response.status_code == 201, response.text
        return await self.register(
            email, organization=None, invite_token=self.outbox.token_for(email)
        )


@dataclass
class Account:
    client: httpx.AsyncClient
    email: str
    session: dict[str, Any]

    @property
    def token(self) -> str:
        return str(self.session["access_token"])

    @property
    def user_id(self) -> str:
        return str(self.session["user"]["id"])

    @property
    def org_id(self) -> str:
        return str(self.session["memberships"][0]["organization_id"])

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.client.get(url, headers=self.headers, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.client.post(url, headers=self.headers, **kwargs)

    async def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.client.patch(url, headers=self.headers, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.client.put(url, headers=self.headers, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.client.delete(url, headers=self.headers, **kwargs)

    # --- shortcuts for project-management tests ---------------------------------------

    @property
    def projects_url(self) -> str:
        return f"/api/v1/organizations/{self.org_id}/projects"

    async def create_project(self, name: str = "Shop") -> dict[str, Any]:
        response = await self.post(self.projects_url, json={"name": name})
        assert response.status_code == 201, response.text
        project: dict[str, Any] = response.json()
        return project

    async def create_application(self, project_id: str, **overrides: Any) -> dict[str, Any]:
        body: dict[str, Any] = {"name": "Storefront", "base_url": "https://acme.io"}
        body.update(overrides)
        response = await self.post(f"{self.projects_url}/{project_id}/applications", json=body)
        assert response.status_code == 201, response.text
        application: dict[str, Any] = response.json()
        return application

    async def create_environment(
        self, project_id: str, application_id: str, **overrides: Any
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": "staging",
            "kind": "staging",
            "base_url": "https://staging.acme.io",
        }
        body.update(overrides)
        response = await self.post(
            f"{self.projects_url}/{project_id}/applications/{application_id}/environments",
            json=body,
        )
        assert response.status_code == 201, response.text
        environment: dict[str, Any] = response.json()
        return environment

    async def setup_environment(self, **environment: Any) -> "Workspace":
        """Project + application + one environment, ready for tests to act on."""
        project = await self.create_project()
        application = await self.create_application(project["id"])
        created = await self.create_environment(project["id"], application["id"], **environment)
        base = f"{self.projects_url}/{project['id']}"
        app_url = f"{base}/applications/{application['id']}"
        return Workspace(
            project_id=project["id"],
            application_id=application["id"],
            environment_id=created["id"],
            project_url=base,
            application_url=app_url,
            environment_url=f"{app_url}/environments/{created['id']}",
        )


@dataclass(frozen=True)
class Workspace:
    project_id: str
    application_id: str
    environment_id: str
    project_url: str
    application_url: str
    environment_url: str

    @property
    def environments_url(self) -> str:
        return f"{self.application_url}/environments"

    @property
    def credentials_url(self) -> str:
        return f"{self.environment_url}/credentials"


@pytest.fixture
async def api(integration_settings: Settings, clean_state: None) -> AsyncIterator[Api]:
    application = create_app(integration_settings)
    outbox = Outbox()
    network = FakeNetwork()
    application.state.email_sender = outbox
    application.state.access_validator = AccessValidator(network)
    application.state.access_probe_transport = httpx.MockTransport(network.handle)
    llm, queue = FakeGateway(), InlineQueue()
    application.state.llm_gateway = llm
    application.state.job_queue = queue
    async with application.router.lifespan_context(application):
        yield Api(app=application, outbox=outbox, network=network, llm=llm, queue=queue)
