"""A stand-in for the test engine service, for tests of everything around it.

It answers a plan with the events a real engine would send, with outcomes the
test chooses per case. The real engine is tested in `tests/engine`.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.engine.client import EngineError
from app.engine.protocol import (
    ArtifactRef,
    CaseFinished,
    CaseStarted,
    Event,
    RunFinished,
    RunPlan,
    RunStarted,
    StepFinished,
)
from app.testing.automation import is_assertion

PNG = b"\x89PNG\r\n\x1a\n" + b"fake screenshot"


@dataclass
class FakeEngine:
    # case key -> "passed" | "failed" | "error" | "flaky" | "transient" | "healed" |
    # "heal_proposed". Others pass.
    # "transient" is an error of the browser itself, the kind that may go away.
    outcomes: dict[str, str] = field(default_factory=dict)
    # case key -> outcomes of successive runs; takes precedence while it lasts.
    sequences: dict[str, list[str]] = field(default_factory=dict)
    # How many main steps pass before a transient error strikes (default: none).
    transient_after_steps: int = 0
    plans: list[RunPlan] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    fetched: list[str] = field(default_factory=list)
    # Test hooks.
    unavailable: EngineError | None = None
    abort_after_cases: int | None = None
    hang_in_case: int | None = None
    stream_closed: bool = False
    missing_artifacts: bool = False
    started: asyncio.Event = field(default_factory=asyncio.Event)
    _healed: set[tuple[str, int]] = field(default_factory=set)
    # What `fetch_artifact` returns, and what the browser console is said to have printed.
    artifact_bytes: bytes = PNG
    console: list[dict[str, Any]] = field(default_factory=list)

    async def run(self, plan: RunPlan) -> AsyncIterator[Event]:
        if self.unavailable is not None:
            raise self.unavailable
        self.plans.append(plan)
        clock = datetime(2026, 1, 1, tzinfo=UTC)
        try:
            yield RunStarted(browser=plan.config.browser, browser_version="fake")
            for index, case in enumerate(plan.cases):
                if self.abort_after_cases is not None and index >= self.abort_after_cases:
                    yield CaseStarted(result_id=case.result_id, attempt=1, started_at=clock)
                    yield RunFinished(
                        status="aborted", error_type="internal", error_message="The engine crashed."
                    )
                    return
                sequence = self.sequences.get(case.key)
                outcome = sequence.pop(0) if sequence else self.outcomes.get(case.key, "passed")
                transient = outcome == "transient"
                # "healed": every step passes, one of them with a replacement locator.
                # "heal_proposed": a locator was not found and a replacement was only proposed.
                healed = outcome == "healed"
                proposed = outcome == "heal_proposed"
                if healed:
                    outcome = "passed"
                if proposed:
                    outcome = "error"
                if transient:
                    outcome = "error"
                attempts = 2 if outcome == "flaky" else 1
                for attempt in range(1, attempts + 1):
                    yield CaseStarted(result_id=case.result_id, attempt=attempt, started_at=clock)
                    if self.hang_in_case == index:
                        self.started.set()
                        await asyncio.sleep(3600)
                    bad = (
                        outcome if attempt == attempts and outcome in ("failed", "error") else None
                    )
                    if outcome == "flaky" and attempt == 1:
                        bad = "failed"
                    artifacts: list[ArtifactRef] = []
                    steps = list(case.script.steps)
                    position = 0
                    if case.script.login_as:
                        position += 1
                        yield self._step(
                            case.result_id,
                            attempt,
                            position,
                            "login",
                            "passed",
                            clock,
                            phase="setup",
                        )
                    stopped = False
                    for number, step in enumerate(steps, start=1):
                        position += 1
                        last = number == len(steps)
                        if transient:
                            last = number == self.transient_after_steps + 1
                        if proposed:
                            last = getattr(step, "locator", None) is not None
                        status = "skipped" if stopped else "passed"
                        artifact = None
                        if bad and last and not stopped:
                            stopped = True
                            status = bad
                            artifact = ArtifactRef(
                                name=f"{case.result_id[:8]}-a{attempt}-001-failure.png",
                                kind="screenshot",
                                content_type="image/png",
                                size_bytes=len(PNG),
                            )
                            artifacts.append(artifact)
                        healing = None
                        locator: Any = getattr(step, "locator", None)
                        usable = locator is not None and not is_assertion(step)
                        if usable and (
                            (
                                healed
                                and status == "passed"
                                and not self._healed_once(case.result_id, attempt)
                            )
                            or (proposed and status == "error")
                        ):
                            healing = {
                                "decision": "recover" if healed else "propose",
                                "verified": healed,
                                "original": locator.model_dump(
                                    exclude_none=True, exclude_defaults=True
                                ),
                                "replacement": _renamed(locator),
                                "confidence": 0.88,
                                "rationale": "The closest match on the page.",
                                "held_back": None if healed else "Not close enough.",
                                "candidates": [],
                            }
                        yield self._step(
                            case.result_id, attempt, position, step.action, status, clock,
                            assertion=is_assertion(step), artifact=artifact, transient=transient,
                            script_index=number - 1, healing=healing,
                        )  # fmt: skip
                    for step in case.script.cleanup:
                        position += 1
                        yield self._step(
                            case.result_id,
                            attempt,
                            position,
                            step.action,
                            "passed",
                            clock,
                            phase="cleanup",
                        )
                    final = attempt == attempts
                    status = bad or ("flaky" if outcome == "flaky" and final else "passed")
                    yield CaseFinished(
                        result_id=case.result_id,
                        attempt=attempt,
                        final=final,
                        status=status,
                        failure_class="infrastructure"
                        if transient
                        else {"failed": "assertion", "error": "locator"}.get(bad or ""),
                        failure_summary=f"{case.key} did not pass" if bad else None,
                        started_at=clock,
                        finished_at=clock + timedelta(seconds=2),
                        duration_ms=2000,
                        artifacts=artifacts,
                        details={
                            "healed_steps": 1 if healed else 0,
                            "heal_proposals": 1 if proposed else 0,
                            "console_log": list(self.console),
                            "console_errors": [
                                m["text"] for m in self.console if m["type"] == "error"
                            ],
                            "cleanup": "ok" if case.script.cleanup else "none",
                            "evidence_notes": [],
                        },
                    )
            yield RunFinished(status="completed")
        finally:
            self.stream_closed = True

    def _healed_once(self, result_id: str, attempt: int) -> bool:
        """True from the second call on for one case attempt: only its first step is healed."""
        key = (result_id, attempt)
        seen = key in self._healed
        self._healed.add(key)
        return seen

    @staticmethod
    def _step(
        result_id: str,
        attempt: int,
        position: int,
        action: str,
        status: str,
        clock: datetime,
        *,
        phase: str = "main",
        assertion: bool = False,
        artifact: ArtifactRef | None = None,
        transient: bool = False,
        script_index: int | None = None,
        healing: dict[str, Any] | None = None,
    ) -> StepFinished:
        failed = status == "failed"
        return StepFinished(
            result_id=result_id,
            attempt=attempt,
            phase=phase,
            position=position,
            action=action,
            status=status,
            started_at=clock,
            finished_at=clock + timedelta(milliseconds=120),
            duration_ms=120,
            locator={"by": "role", "matched": 1} if action in ("click", "fill") else None,
            expected={"equals": "50"} if assertion else None,
            actual={"text": "51" if failed else "50"}
            if assertion and status != "skipped"
            else None,
            error_type="internal"
            if transient and status == "error"
            else {"failed": "assertion", "error": "locator_not_found"}.get(status),
            error_message="expected 50, found 51"
            if failed
            else "No element matches"
            if status == "error"
            else None,
            page_url="https://staging.acme.io/loyalty",
            artifact=artifact,
            script_index=script_index,
            healing=healing,
        )

    async def fetch_artifact(self, run_id: str, name: str) -> bytes:
        self.fetched.append(name)
        if self.missing_artifacts:
            raise EngineError("artifact_missing", f"The engine has no file named {name}.")
        return self.artifact_bytes

    async def delete_run(self, run_id: str) -> None:
        self.deleted.append(run_id)

    async def health(self) -> dict[str, Any]:
        return {"status": "ok"}


def _renamed(locator: Any) -> dict[str, Any]:
    """The same locator under a slightly different name, as a renamed element would need."""
    record: dict[str, Any] = locator.model_dump(exclude_none=True, exclude_defaults=True)
    field_name = "name" if record.get("by") == "role" else "value"
    record[field_name] = f"{record.get(field_name, '')} (new)".strip()
    record["exact"] = True
    return record
