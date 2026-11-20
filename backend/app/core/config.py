"""Environment-based application settings.

Every value comes from environment variables. Connection strings and other
secrets have no defaults: the process refuses to start without them.
"""

from decimal import Decimal
from functools import lru_cache
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Environment = Literal["development", "test", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    # Application
    app_name: str = "qa-agent"
    app_version: str = "0.1.0"
    app_env: Environment = "development"
    log_level: LogLevel = "INFO"
    api_v1_prefix: str = "/api/v1"

    # PostgreSQL (required)
    database_url: str = Field(repr=False)
    migration_database_url: str | None = Field(default=None, repr=False)
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=100)

    # Redis (required)
    redis_url: str = Field(repr=False)

    # HTTP
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # Health
    readiness_timeout_s: float = Field(default=2.0, gt=0, le=30)

    # Authentication (the signing key is required and has no default)
    jwt_secret_key: SecretStr = Field(min_length=32)
    access_token_ttl_s: int = Field(default=900, ge=60, le=3600)
    refresh_token_ttl_s: int = Field(default=30 * 24 * 3600, ge=3600)
    password_reset_ttl_s: int = Field(default=3600, ge=300, le=24 * 3600)
    invitation_ttl_s: int = Field(default=7 * 24 * 3600, ge=3600)
    auth_cookie_name: str = "qa_refresh_token"
    # Must stay true anywhere the site is served over HTTPS; false only for local HTTP.
    auth_cookie_secure: bool = True
    # Base URL of the web app, used to build links in emails.
    public_base_url: str = "http://localhost:5173"

    # Credential encryption: "<version>:<base64 of 32 random bytes>", comma-separated.
    # Required. The highest version encrypts; all listed versions can decrypt.
    credential_master_keys: SecretStr

    # Reachability checks of applications under test
    access_probe_timeout_s: float = Field(default=5.0, gt=0, le=30)
    access_probe_max_redirects: int = Field(default=5, ge=0, le=10)
    access_probe_limit: int = Field(default=30, ge=1)
    access_probe_window_s: int = Field(default=60, ge=10)

    # AI service. Optional: without a key the platform runs, but AI features answer 503.
    # Which service answers: "openai" (OPENAI_API_KEY) or "gemini" (GEMINI_API_KEY, Google's
    # native API). Only the chosen provider's key is used.
    llm_provider: Literal["openai", "gemini"] = "openai"
    openai_api_key: SecretStr | None = None
    openai_base_url: str | None = None
    gemini_api_key: SecretStr | None = None
    # Client-side pacing per process; 0 = off. Set below the provider's requests-per-minute
    # quota, remembering that the API and the worker each count on their own.
    llm_requests_per_minute: int = Field(default=0, ge=0, le=10_000)
    # Model used for requirement analysis. Required whenever a key is set; there is
    # deliberately no default, so the choice of model is always explicit.
    llm_model_analysis: str | None = None
    # Model used to write test cases. Falls back to the analysis model when unset.
    llm_model_generation: str | None = None
    llm_generation_max_output_tokens: int = Field(default=16000, ge=1000, le=64000)
    # Test-case generation: objectives per model call, and cases allowed per call and in total.
    generation_objectives_per_call: int = Field(default=4, ge=1, le=20)
    generation_max_cases_per_call: int = Field(default=12, ge=1, le=40)
    generation_max_cases: int = Field(default=60, ge=1, le=200)
    llm_timeout_s: float = Field(default=60.0, gt=0, le=600)
    llm_max_attempts: int = Field(default=3, ge=1, le=6)
    llm_retry_base_delay_s: float = Field(default=1.0, ge=0, le=30)
    llm_retry_max_delay_s: float = Field(default=20.0, ge=0, le=120)
    llm_max_output_tokens: int = Field(default=4000, ge=256, le=32000)
    # Not sent unless set: some models only accept their default temperature.
    llm_temperature: float | None = Field(default=None, ge=0, le=2)
    # USD per million tokens. Cost is tracked only when both are set.
    llm_input_price_per_mtok: Decimal | None = Field(default=None, ge=0)
    llm_output_price_per_mtok: Decimal | None = Field(default=None, ge=0)
    # Spending cap per organization and calendar month, if prices are configured.
    llm_monthly_budget_usd: Decimal | None = Field(default=None, ge=0)
    # Limits on requirement analysis
    instruction_max_chars: int = Field(default=4000, ge=100, le=20000)
    instruction_max_llm_calls: int = Field(default=6, ge=1, le=50)
    instruction_limit: int = Field(default=30, ge=1)
    instruction_window_s: int = Field(default=3600, ge=60)
    # After this long an analysis that never finished may be started again.
    analysis_stale_after_s: int = Field(default=300, ge=30)

    # Rate limits (attempts per window)
    login_max_failures: int = Field(default=5, ge=1)
    login_failure_window_s: int = Field(default=900, ge=60)
    login_ip_limit: int = Field(default=30, ge=1)
    login_ip_window_s: int = Field(default=300, ge=10)
    register_ip_limit: int = Field(default=10, ge=1)
    register_ip_window_s: int = Field(default=3600, ge=60)
    password_reset_limit: int = Field(default=3, ge=1)
    password_reset_window_s: int = Field(default=3600, ge=60)

    # Browser automation. Optional: without RUNNER_URL browser features answer "not
    # configured". Only the worker starts browsers, so only it needs the tokens and
    # the egress addresses; the API needs RUNNER_URL alone, to know the feature exists.
    runner_url: str | None = None
    runner_token: SecretStr | None = Field(default=None, min_length=24)
    egress_admin_url: str | None = None
    egress_admin_token: SecretStr | None = Field(default=None, min_length=24)
    egress_proxy_url: str | None = None
    browser_name: Literal["chromium", "firefox", "webkit"] = "chromium"
    browser_action_timeout_ms: int = Field(default=5_000, ge=500, le=60_000)
    browser_navigation_timeout_ms: int = Field(default=30_000, ge=1_000, le=120_000)
    browser_session_ttl_s: int = Field(default=300, ge=30, le=3_600)
    browser_check_timeout_s: float = Field(default=120.0, gt=0, le=1_800)
    browser_check_limit: int = Field(default=20, ge=1)
    browser_check_window_s: int = Field(default=3_600, ge=1)
    browser_check_result_ttl_s: int = Field(default=3_600, ge=60)

    # Test engine. ENGINE_URL tells the API the feature exists; only the worker has the
    # token and talks to the engine.
    engine_url: str | None = None
    engine_token: SecretStr | None = Field(default=None, min_length=24)
    # Where the worker stores evidence files and the API reads them from.
    evidence_root: str = "/var/lib/qa-agent/evidence"
    evidence_retention_days: int = Field(default=30, ge=1)
    # Videos and traces are large and show every moment of a run: kept for less long.
    evidence_recording_retention_days: int = Field(default=14, ge=1)
    evidence_max_screenshot_bytes: int = Field(default=10 * 1024 * 1024, ge=1)
    evidence_max_recording_bytes: int = Field(default=100 * 1024 * 1024, ge=1)
    evidence_max_log_bytes: int = Field(default=1024 * 1024, ge=1)
    # All evidence of one execution together.
    evidence_max_execution_bytes: int = Field(default=1024 * 1024 * 1024, ge=1)
    # How long a download address for one evidence file stays valid.
    evidence_url_ttl_s: int = Field(default=300, ge=30, le=3_600)
    # Outgoing mail (invitations, password resets). Without a host nothing is sent.
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None
    # Largest request body the API reads. Uploads of test files are base64 inside JSON.
    max_request_body_bytes: int = Field(default=4 * 1024 * 1024, ge=64 * 1024)
    # Bearer token for GET /metrics. Without one the endpoint does not exist.
    metrics_token: SecretStr | None = Field(default=None, min_length=24)
    # A running execution whose worker has not been heard from for this long is closed.
    execution_stale_after_s: int = Field(default=90, ge=30)
    # GitHub. The API address is fixed by the installation (GitHub Enterprise Server has
    # its own); it is never taken from a request.
    github_api_url: str = "https://api.github.com"
    github_timeout_s: float = Field(default=15.0, gt=0, le=60)
    github_webhook_max_bytes: int = Field(default=1024 * 1024, ge=1024)
    # Requests one API key may make per minute.
    ci_rate_limit: int = Field(default=120, ge=1)
    execution_timeout_s: int = Field(default=1_800, ge=30, le=14_400)
    execution_max_cases: int = Field(default=200, ge=1, le=500)
    # Executions of one organization that may be queued or running at the same time.
    execution_max_active: int = Field(default=3, ge=1)
    execution_limit: int = Field(default=60, ge=1)
    execution_window_s: int = Field(default=3_600, ge=1)
    test_file_max_bytes: int = Field(default=2 * 1024 * 1024, ge=1)
    # Agent workflow runs of one organization that may be working or waiting at once.
    workflow_max_open: int = Field(default=10, ge=1)

    @field_validator("database_url", "migration_database_url")
    @classmethod
    def _require_asyncpg(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("postgresql+asyncpg://"):
            raise ValueError("must use the postgresql+asyncpg:// scheme")
        return value

    @field_validator("redis_url")
    @classmethod
    def _require_redis_scheme(cls, value: str) -> str:
        if not value.startswith(("redis://", "rediss://")):
            raise ValueError("must use the redis:// or rediss:// scheme")
        return value

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("cors_allowed_origins")
    @classmethod
    def _reject_wildcard(cls, value: list[str]) -> list[str]:
        if "*" in value:
            raise ValueError("wildcard origin is not allowed; list origins explicitly")
        return [origin.rstrip("/") for origin in value]

    @field_validator("credential_master_keys")
    @classmethod
    def _valid_master_keys(cls, value: SecretStr) -> SecretStr:
        from app.core.security.crypto import parse_master_keys

        parse_master_keys(value.get_secret_value())
        return value

    @field_validator(
        "openai_api_key",
        "openai_base_url",
        "gemini_api_key",
        "llm_model_analysis",
        "llm_model_generation",
        "llm_temperature",
        "llm_input_price_per_mtok",
        "llm_output_price_per_mtok",
        "llm_monthly_budget_usd",
        "runner_url",
        "runner_token",
        "egress_admin_url",
        "egress_admin_token",
        "egress_proxy_url",
        "engine_url",
        "engine_token",
        "metrics_token",
        "smtp_host",
        "smtp_username",
        "smtp_password",
        "smtp_from",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # Compose passes unset variables as empty strings.
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def _llm_requirements(self) -> Self:
        if self.llm_api_key is not None and not self.llm_model_analysis:
            raise ValueError("llm_model_analysis must be set when the model provider's key is set")
        return self

    @model_validator(mode="after")
    def _production_requirements(self) -> Self:
        if self.app_env == "production" and not self.auth_cookie_secure:
            raise ValueError("auth_cookie_secure must be true in production")
        return self

    @property
    def llm_api_key(self) -> SecretStr | None:
        """The key of the chosen provider. The other provider's key is ignored."""
        return self.gemini_api_key if self.llm_provider == "gemini" else self.openai_api_key

    @property
    def auth_cookie_path(self) -> str:
        """The refresh cookie is only ever sent to the authentication endpoints."""
        return f"{self.api_v1_prefix}/auth"

    @property
    def browser_configured(self) -> bool:
        return self.runner_url is not None

    @property
    def can_start_browsers(self) -> bool:
        return all(
            (
                self.runner_url,
                self.runner_token,
                self.egress_admin_url,
                self.egress_admin_token,
                self.egress_proxy_url,
            )
        )

    @property
    def engine_configured(self) -> bool:
        return self.engine_url is not None

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
