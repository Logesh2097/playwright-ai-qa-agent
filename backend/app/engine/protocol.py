"""What the worker and the engine say to each other.

The worker sends one `RunPlan`. The engine answers with a stream of events, one
JSON object per line, ending with `run_finished`. Evidence files are fetched
afterwards by name.
"""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter

from app.testing.automation import Script

EvidenceMode = Literal["off", "on_failure", "always"]
StepStatus = Literal["passed", "failed", "error", "skipped"]
CaseStatus = Literal["passed", "failed", "error", "flaky"]
Phase = Literal["setup", "main", "cleanup"]
ArtifactKind = Literal["screenshot", "video", "trace"]
ARTIFACT_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$"


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- configuration (also what the API accepts and an execution stores) --------------------


class ViewportSize(_Wire):
    width: int = Field(default=1280, ge=320, le=3840)
    height: int = Field(default=720, ge=240, le=2160)

    @property
    def label(self) -> str:
        return f"{self.width}x{self.height}"


class Timeouts(_Wire):
    action_ms: int = Field(default=5_000, ge=500, le=60_000)
    navigation_ms: int = Field(default=30_000, ge=1_000, le=120_000)
    test_ms: int = Field(
        default=120_000, ge=5_000, le=900_000, description="Per case, per attempt."
    )


class RetryPolicy(_Wire):
    """A case that passes only on a retry is reported as flaky, never as passed."""

    max_attempts: int = Field(default=1, ge=1, le=3)
    retry_on: list[Literal["error", "failed"]] = Field(
        default=["error"],
        description="error: the step could not be performed. failed: an assertion did not hold.",
    )


class EvidenceSettings(_Wire):
    screenshots: EvidenceMode = "on_failure"
    video: EvidenceMode = "off"
    trace: EvidenceMode = "off"
    # Console and network logs as files. Their error lines are always kept with the result.
    logs: EvidenceMode = "on_failure"
    full_page: bool = False


class HealingSettings(_Wire):
    """What to do when a locator matches nothing (see `app.engine.healing`).

    off      fail the step, as written
    propose  look for a replacement and record it for review; still fail the step
    recover  additionally use the replacement when it is reliable enough, and carry on
    """

    mode: Literal["off", "propose", "recover"] = "off"
    max_per_case: int = Field(default=3, ge=1, le=5)


class RunConfig(_Wire):
    browser: Literal["chromium", "firefox", "webkit"] = "chromium"
    viewport: ViewportSize = Field(default_factory=ViewportSize)
    timeouts: Timeouts = Field(default_factory=Timeouts)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    evidence: EvidenceSettings = Field(default_factory=EvidenceSettings)
    healing: HealingSettings = Field(default_factory=HealingSettings)


# --- the plan -----------------------------------------------------------------------------


class ProxySettings(_Wire):
    server: str
    username: str
    password: str


class CredentialGrant(_Wire):
    """A decrypted test credential, for the duration of one run."""

    fields: dict[str, str]
    login_recipe: list[dict[str, Any]] = Field(default_factory=list)


class FileGrant(_Wire):
    name: str
    content_type: str
    content_base64: str


class PlannedCase(_Wire):
    result_id: str
    key: str
    title: str
    script: Script
    data: dict[str, str] = Field(default_factory=dict)


class RunPlan(_Wire):
    run_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f-]{36}$")]
    config: RunConfig
    base_url: str
    allowed_origins: list[str] = Field(min_length=1, max_length=50)
    allow_http: bool = True
    read_only: bool = False
    allowed_actions: list[str]
    proxy: ProxySettings | None = None
    env: dict[str, str] = Field(default_factory=dict)
    credentials: dict[str, CredentialGrant] = Field(default_factory=dict)
    files: list[FileGrant] = Field(default_factory=list, max_length=50)
    cases: list[PlannedCase] = Field(min_length=1, max_length=500)


# --- events -------------------------------------------------------------------------------


class ArtifactRef(_Wire):
    name: Annotated[str, StringConstraints(pattern=ARTIFACT_NAME)]
    kind: ArtifactKind
    content_type: str
    size_bytes: int


class RunStarted(_Wire):
    event: Literal["run_started"] = "run_started"
    browser: str
    browser_version: str


class CaseStarted(_Wire):
    event: Literal["case_started"] = "case_started"
    result_id: str
    attempt: int
    started_at: datetime


class StepFinished(_Wire):
    event: Literal["step_finished"] = "step_finished"
    result_id: str
    attempt: int
    phase: Phase
    position: int
    # Which step of the script this is (0-based, within its phase). None for sign-in.
    script_index: int | None = None
    action: str
    description: str | None = None
    status: StepStatus
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    locator: dict[str, Any] | None = None
    expected: dict[str, Any] | None = None
    actual: dict[str, Any] | None = None
    error_type: str | None = None
    error_message: str | None = None
    page_url: str | None = None
    artifact: ArtifactRef | None = None
    # Present when the step's locator matched nothing and a replacement was looked for.
    healing: dict[str, Any] | None = None


class CaseFinished(_Wire):
    event: Literal["case_finished"] = "case_finished"
    result_id: str
    attempt: int
    # True for the attempt that decides the case; earlier attempts were retried.
    final: bool
    status: CaseStatus
    failure_class: str | None = None
    failure_summary: str | None = None
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class RunFinished(_Wire):
    event: Literal["run_finished"] = "run_finished"
    status: Literal["completed", "aborted"]
    error_type: str | None = None
    error_message: str | None = None


Event = Annotated[
    RunStarted | CaseStarted | StepFinished | CaseFinished | RunFinished,
    Field(discriminator="event"),
]
EVENT: TypeAdapter[Event] = TypeAdapter(Event)

# How the engine's reasons for a step not passing map to the failure classes that
# results are filed under.
FAILURE_CLASS: dict[str, str] = {
    "assertion": "assertion",
    "locator_not_found": "locator",
    "locator_ambiguous": "locator",
    "timeout": "timeout",
    "test_timeout": "timeout",
    "navigation": "navigation",
    "destination_not_allowed": "network",
    "request_failed": "network",
    "test_data": "test_data",
    "action_not_allowed": "environment",
    "environment_read_only": "environment",
    "login_failed": "environment",
    "action_failed": "unknown",
    "internal": "infrastructure",
}
