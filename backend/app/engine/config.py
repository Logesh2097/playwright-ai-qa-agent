"""Settings of the engine service. It needs no database, Redis or application secrets."""

import tempfile

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class EngineSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    log_level: str = "INFO"
    engine_token: SecretStr = Field(min_length=24)
    engine_max_runs: int = Field(default=2, ge=1, le=32)
    # Upper limit for one run, whatever its plan asks for.
    engine_max_run_seconds: int = Field(default=3_600, ge=30)
    # How long evidence of a finished run is kept for the worker to collect.
    engine_artifact_ttl_s: int = Field(default=900, ge=30)
    engine_work_dir: str = Field(default_factory=lambda: tempfile.gettempdir() + "/runs")
    engine_require_proxy: bool = True
