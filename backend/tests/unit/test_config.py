import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.main import create_app
from app.worker.jobs import ping
from tests.conftest import make_settings


def test_missing_required_settings_fail_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    monkeypatch.delenv("CREDENTIAL_MASTER_KEYS", raising=False)
    with pytest.raises(ValidationError) as excinfo:
        Settings()
    missing = {error["loc"][0] for error in excinfo.value.errors()}
    assert missing == {"database_url", "redis_url", "jwt_secret_key", "credential_master_keys"}


def test_settings_are_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/app")
    monkeypatch.setenv("REDIS_URL", "redis://cache:6379/0")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "true")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://a.example, https://b.example/")
    settings = Settings()
    assert settings.is_production
    assert settings.cors_allowed_origins == ["https://a.example", "https://b.example"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"database_url": "postgresql://u:p@db/app"},
        {"redis_url": "http://cache:6379"},
        {"cors_allowed_origins": "*"},
        {"jwt_secret_key": "too-short"},
        {"credential_master_keys": "1:not-base64!"},
        {"credential_master_keys": "1:c2hvcnQ="},
        {"credential_master_keys": ""},
        {"app_env": "production", "auth_cookie_secure": False},
        {"app_env": "staging"},
    ],
)
def test_invalid_settings_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_settings(**overrides)


def test_secrets_are_not_in_repr() -> None:
    settings = make_settings(
        database_url="postgresql+asyncpg://u:topsecret@db/app",
        jwt_secret_key="signing-key-that-must-stay-private-123",
    )
    assert "topsecret" not in repr(settings)
    assert "must-stay-private" not in repr(settings)


def test_refresh_cookie_is_scoped_to_auth_endpoints() -> None:
    assert make_settings().auth_cookie_path == "/api/v1/auth"
    assert make_settings().auth_cookie_secure is True


def test_api_docs_are_disabled_in_production() -> None:
    assert create_app(make_settings(app_env="production")).openapi_url is None
    assert create_app(make_settings(app_env="development")).openapi_url == "/api/openapi.json"


async def test_ping_job() -> None:
    assert await ping({}) == "pong"
