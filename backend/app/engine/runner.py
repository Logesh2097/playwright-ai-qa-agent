"""Running a plan: for every case, sign in if asked, run the steps, clean up, retry if allowed.

How a case attempt is judged, entirely from what its steps did:

    error    a step could not be performed (no such element, time ran out,
             address not allowed, sign-in failed). Nothing is known about the
             application's behaviour.
    failed   every step could be performed and at least one assertion did not hold.
    passed   every step was performed and every assertion held, and at least
             one assertion ran.

A case that needed a retry to pass is reported as `flaky`.
"""

import asyncio
import base64
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from playwright.async_api import BrowserContext, Page
from playwright.async_api import Error as PlaywrightError
from pydantic import BaseModel, ValidationError

from app.browser.login import LoginRecipe
from app.browser.redaction import Redactor
from app.engine.actions import ACTIONS
from app.engine.assertions import ASSERTIONS
from app.engine.browser import BrowserManager, PageLog
from app.engine.errors import StepError, StepFailure
from app.engine.evidence import Evidence
from app.engine.healing import Healing, find_replacement
from app.engine.login import sign_in
from app.engine.protocol import (
    FAILURE_CLASS,
    ArtifactRef,
    CaseFinished,
    CaseStarted,
    CaseStatus,
    Phase,
    PlannedCase,
    RunFinished,
    RunPlan,
    RunStarted,
    StepFinished,
    StepStatus,
)
from app.engine.steps import StepContext, StepOutcome, first_line
from app.engine.values import Values
from app.testing.automation import changes_state, is_assertion, permission_for

logger = logging.getLogger(__name__)

Emit = Callable[[dict[str, Any]], Awaitable[None]]
_CLEANUP_BUDGET_S = 60.0
_MAX_VALUE_CHARS = 2_000
HANDLERS = {**ACTIONS, **ASSERTIONS}


def _now() -> datetime:
    return datetime.now(UTC)


def _compact(value: Any) -> Any:
    """Observed values can be whole documents; keep what explains the outcome."""
    if value is None:
        return None
    text = json.dumps(value, default=str)
    if len(text) <= _MAX_VALUE_CHARS:
        return json.loads(text)
    return {"truncated": text[:_MAX_VALUE_CHARS], "size": len(text)}


@dataclass(slots=True)
class _Attempt:
    """Running totals of one case attempt."""

    position: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    assertions_held: int = 0
    halted: bool = False
    cleanup: str = "none"
    artifacts: list[ArtifactRef] = field(default_factory=list)
    # Steps that only worked with a replacement locator, and replacements only proposed.
    healed: int = 0
    proposed: int = 0

    @property
    def status(self) -> str:
        if self.errors:
            return "error"
        if self.failures:
            return "failed"
        return "passed"


class PlanRunner:
    def __init__(self, plan: RunPlan, run_dir: Path, emit: Emit) -> None:
        self._plan = plan
        self._dir = run_dir
        self._raw_emit = emit
        secrets = {
            value
            for grant in plan.credentials.values()
            for value in Values(env={}, data={}, credentials={"x": grant.fields}).secrets
        }
        if plan.proxy is not None:
            secrets.add(plan.proxy.password)
        self._redactor = Redactor(secrets)
        self._files: dict[str, Path] = {}
        self._allowed_actions = frozenset(plan.allowed_actions)

    async def _emit(self, event: BaseModel) -> None:
        """Nothing leaves the engine without passing the redactor."""
        await self._raw_emit(self._redactor.value(event.model_dump(mode="json")))

    def _write_files(self) -> None:
        directory = self._dir / "files"
        directory.mkdir(exist_ok=True)
        for index, grant in enumerate(self._plan.files):
            # Stored under a name the engine chooses; the display name is only a key.
            suffix = Path(grant.name).suffix[:10]
            path = directory / f"file-{index}{suffix}"
            path.write_bytes(base64.b64decode(grant.content_base64))
            self._files[grant.name] = path

    async def run(self) -> None:
        self._write_files()
        async with BrowserManager(self._plan) as browser:
            await self._emit(
                RunStarted(browser=self._plan.config.browser, browser_version=browser.version)
            )
            for case in self._plan.cases:
                await self._run_case(browser, case)
        await self._emit(RunFinished(status="completed"))

    # --- one case -------------------------------------------------------------------------

    async def _run_case(self, browser: BrowserManager, case: PlannedCase) -> None:
        retry = self._plan.config.retry
        for number in range(1, retry.max_attempts + 1):
            started = _now()
            await self._emit(
                CaseStarted(result_id=case.result_id, attempt=number, started_at=started)
            )
            attempt, log, notes = await self._attempt(browser, case, number)
            status = attempt.status
            final = (
                number == retry.max_attempts or status == "passed" or status not in retry.retry_on
            )
            if final and status == "passed" and number > 1:
                status = "flaky"
            summary: str | None = None
            failure_class: str | None = None
            if attempt.errors:
                failure_class = FAILURE_CLASS.get(attempt.errors[0][0], "unknown")
                summary = attempt.errors[0][1]
            elif attempt.failures:
                failure_class, summary = "assertion", attempt.failures[0]
            finished = _now()
            await self._emit(
                CaseFinished(
                    result_id=case.result_id,
                    attempt=number,
                    final=final,
                    status=cast(CaseStatus, status),
                    failure_class=failure_class,
                    failure_summary=summary,
                    started_at=started,
                    finished_at=finished,
                    duration_ms=int((finished - started).total_seconds() * 1000),
                    artifacts=attempt.artifacts,
                    details={
                        **log.as_dict(),
                        "cleanup": attempt.cleanup,
                        "assertions_held": attempt.assertions_held,
                        "assertions_failed": len(attempt.failures),
                        "healed_steps": attempt.healed,
                        "heal_proposals": attempt.proposed,
                        "evidence_notes": notes,
                    },
                )
            )
            if final:
                return

    async def _attempt(
        self, browser: BrowserManager, case: PlannedCase, number: int
    ) -> tuple[_Attempt, PageLog, list[str]]:
        plan, script = self._plan, case.script
        attempt = _Attempt()
        values = Values(
            env=plan.env,
            data=case.data,
            credentials={role: grant.fields for role, grant in plan.credentials.items()},
        )
        # A case that types a secret itself (a login test) gets no video or trace: both
        # would record the value. Screenshots stay, with the field painted over.
        types_secret = any(values.uses_secret(step) for step in (*script.steps, *script.cleanup))
        evidence = Evidence(
            self._dir,
            plan.config.evidence,
            tag=f"{case.result_id[:8]}-a{number}",
            allow_recordings=not types_secret,
        )
        if types_secret and (
            plan.config.evidence.video != "off" or plan.config.evidence.trace != "off"
        ):
            evidence.notes.append("recordings_skipped_secret_input")
        log = PageLog()
        context: BrowserContext | None = None
        page: Page | None = None
        try:
            state = await self._sign_in(browser, case, number, attempt)
            if attempt.errors:
                return attempt, log, evidence.notes
            context, log = await browser.new_context(
                storage_state=state, video_dir=evidence.video_dir
            )
            page = await context.new_page()
            await evidence.start_trace(context)
            ctx = StepContext(
                page=page,
                context=context,
                values=values,
                scope=browser.scope,
                evidence=evidence,
                timeouts=plan.config.timeouts,
                allowed_actions=self._allowed_actions,
                files=self._files,
            )
            await self._main(case, number, attempt, ctx)
            await self._cleanup(case, number, attempt, ctx)
        except PlaywrightError as exc:
            attempt.errors.append(("internal", f"The browser failed: {first_line(exc)}"))
        finally:
            passed = attempt.status == "passed"
            if context is not None:
                with contextlib.suppress(PlaywrightError):
                    trace = await evidence.stop_trace(context, passed=passed)
                    if trace is not None:
                        attempt.artifacts.append(trace)
                with contextlib.suppress(PlaywrightError):
                    await context.close()
                video = await evidence.collect_video(page, passed=passed)
                if video is not None:
                    attempt.artifacts.append(video)
        if attempt.status == "passed" and attempt.assertions_held == 0:
            attempt.errors.append(("internal", "No assertion ran, so nothing was verified."))
        return attempt, log, evidence.notes

    # --- phases ---------------------------------------------------------------------------

    async def _sign_in(
        self, browser: BrowserManager, case: PlannedCase, number: int, attempt: _Attempt
    ) -> Any:
        role = case.script.login_as
        if role is None:
            return None
        started = _now()
        state: Any = None
        error: StepError | None = None
        grant = self._plan.credentials.get(role)
        try:
            if grant is None:
                raise StepError(
                    "login_failed", f"No credential was provided for the role '{role}'."
                )
            try:
                recipe = LoginRecipe.model_validate(grant.login_recipe)
            except ValidationError:
                raise StepError(
                    "login_failed", f"The credential '{role}' has no usable login recipe."
                ) from None
            # Its own context, without recording, closed as soon as the session exists.
            context, _ = await browser.new_context()
            try:
                state = await sign_in(
                    context,
                    browser.scope,
                    recipe,
                    grant.fields,
                    self._plan.config.timeouts,
                )
            finally:
                await context.close()
        except StepError as exc:
            error = exc
        except PlaywrightError as exc:
            error = StepError("login_failed", f"Signing in failed: {first_line(exc)}")
        await self._record(
            case, number, attempt, "setup", "login", f"Sign in as {role}", started, error=error
        )
        return state

    async def _main(
        self, case: PlannedCase, number: int, attempt: _Attempt, ctx: StepContext
    ) -> None:
        budget = self._plan.config.timeouts.test_ms / 1000
        running: tuple[Any, datetime] | None = None
        steps = list(case.script.steps)
        index = 0
        try:
            async with asyncio.timeout(budget):
                while index < len(steps) and not attempt.halted:
                    step = steps[index]
                    running = (step, _now())
                    await self._step(case, number, attempt, ctx, "main", step, running[1], index)
                    running = None
                    index += 1
        except TimeoutError:
            if running is not None:
                step, started = running
                stopped = _now()
                await self._record(
                    case, number, attempt, "main", step.action, step.description, started,
                    error=StepError("test_timeout", f"The case did not finish within {budget:g} seconds."),
                    artifact=await self._failure_shot(ctx), finished=stopped, script_index=index,
                )  # fmt: skip
                index += 1
            attempt.halted = True
        for offset, step in enumerate(steps[index:]):
            await self._record(
                case, number, attempt, "main", step.action, step.description, _now(),
                skipped=True, script_index=index + offset,
            )  # fmt: skip

    async def _cleanup(
        self, case: PlannedCase, number: int, attempt: _Attempt, ctx: StepContext
    ) -> None:
        """Always attempted, step by step, whatever happened before. Never changes the verdict."""
        if not case.script.cleanup:
            return
        attempt.cleanup = "ok"
        before = len(attempt.errors)
        try:
            async with asyncio.timeout(_CLEANUP_BUDGET_S):
                for index, step in enumerate(case.script.cleanup):
                    await self._step(case, number, attempt, ctx, "cleanup", step, _now(), index)
        except TimeoutError:
            attempt.cleanup = "failed"
        if len(attempt.errors) > before:
            attempt.cleanup = "failed"
            del attempt.errors[before:]

    async def _step(
        self,
        case: PlannedCase,
        number: int,
        attempt: _Attempt,
        ctx: StepContext,
        phase: Phase,
        step: Any,
        started: datetime,
        script_index: int | None = None,
    ) -> None:
        outcome: StepOutcome | None = None
        error: StepError | None = None
        failure: StepFailure | None = None
        healing: Healing | None = None
        try:
            self._permit(step)
            outcome = await HANDLERS[step.action](step, ctx)
        except StepFailure as exc:
            failure = exc
        except StepError as exc:
            error = exc
        except PlaywrightError as exc:
            error = StepError("action_failed", first_line(exc))
        if error is not None and error.error_type == "locator_not_found" and phase == "main":
            healing, outcome = await self._heal(step, ctx, attempt)
            if healing is not None and healing.verified:
                error = None
        # The step ends here; evidence taken afterwards is not part of its duration.
        finished = _now()
        artifact = outcome.artifact if outcome else None
        mode = self._plan.config.evidence.screenshots
        if (error or failure) and phase == "main":
            artifact = await self._failure_shot(ctx)
        elif mode == "always" and phase == "main" and artifact is None:
            artifact = await ctx.evidence.screenshot(ctx.page, step.action)
        await self._record(
            case, number, attempt, phase, step.action, step.description, started,
            outcome=outcome, error=error, failure=failure, artifact=artifact, page_url=ctx.page.url,
            soft=bool(getattr(step, "continue_on_failure", False)), assertion=is_assertion(step),
            finished=finished, script_index=script_index, healing=healing,
        )  # fmt: skip

    async def _heal(
        self, step: Any, ctx: StepContext, attempt: _Attempt
    ) -> tuple[Healing | None, StepOutcome | None]:
        """Look for the element a failed locator was written for; use it if that is safe.

        Only the locator is ever replaced. The step is then performed exactly as
        written, so an assertion still compares with its original expected value.
        """
        settings = self._plan.config.healing
        locator = getattr(step, "locator", None)
        if settings.mode == "off" or locator is None:
            return None, None
        try:
            healing = await find_replacement(
                ctx.page, action=step.action, locator=locator, values=ctx.values
            )
        except PlaywrightError:
            return None, None
        if healing.decision == "none":
            return healing, None
        if healing.decision == "recover" and settings.mode == "recover":
            if attempt.healed >= settings.max_per_case:
                healing.decision = "propose"
                healing.held_back = (
                    f"{attempt.healed} steps of this case were already recovered; "
                    "more than that needs a person."
                )
            else:
                try:
                    outcome = await HANDLERS[step.action](
                        step.model_copy(update={"locator": healing.replacement}), ctx
                    )
                except (StepFailure, StepError, PlaywrightError):
                    # With the replacement the step still does not pass. Whether that is
                    # the application or a wrong guess cannot be told, so the original
                    # error stands and the replacement is only a proposal.
                    healing.decision = "propose"
                    healing.held_back = "The step did not pass with the replacement either."
                else:
                    healing.verified = True
                    attempt.healed += 1
                    return healing, outcome
        elif healing.decision == "recover":
            healing.decision = "propose"
            healing.held_back = "Automatic recovery is switched off for this run."
        attempt.proposed += 1
        return healing, None

    def _permit(self, step: Any) -> None:
        """The environment's limits, enforced here as well as when the plan was built."""
        permission = permission_for(step)
        if permission not in self._allowed_actions:
            raise StepError(
                "action_not_allowed", f"The environment does not allow '{permission}' actions."
            )
        if self._plan.read_only and changes_state(step):
            raise StepError(
                "environment_read_only",
                f"'{step.action}' can change data; the environment is read-only.",
            )

    async def _failure_shot(self, ctx: StepContext) -> ArtifactRef | None:
        if self._plan.config.evidence.screenshots == "off":
            return None
        return await ctx.evidence.screenshot(ctx.page, "failure")

    async def _record(
        self,
        case: PlannedCase,
        number: int,
        attempt: _Attempt,
        phase: Phase,
        action: str,
        description: str | None,
        started: datetime,
        *,
        outcome: StepOutcome | None = None,
        error: StepError | None = None,
        failure: StepFailure | None = None,
        artifact: ArtifactRef | None = None,
        page_url: str | None = None,
        skipped: bool = False,
        soft: bool = False,
        assertion: bool = False,
        finished: datetime | None = None,
        script_index: int | None = None,
        healing: Healing | None = None,
    ) -> None:
        attempt.position += 1
        status: StepStatus = "passed"
        if skipped:
            status = "skipped"
        elif error is not None:
            status = "error"
            attempt.errors.append((error.error_type, error.message))
            attempt.halted = True
        elif failure is not None:
            status = "failed"
            attempt.failures.append(failure.message)
            attempt.halted = attempt.halted or not soft
        elif assertion:
            attempt.assertions_held += 1
        if artifact is not None:
            attempt.artifacts.append(artifact)
        finished = finished or _now()
        await self._emit(
            StepFinished(
                result_id=case.result_id,
                attempt=number,
                phase=phase,
                position=attempt.position,
                script_index=script_index,
                action=action,
                description=description,
                status=status,
                started_at=started,
                finished_at=finished,
                duration_ms=int((finished - started).total_seconds() * 1000),
                locator=outcome.locator if outcome else None,
                expected=_compact(
                    failure.expected if failure else outcome.expected if outcome else None
                ),
                actual=_compact(failure.actual if failure else outcome.actual if outcome else None),
                error_type="assertion" if failure else error.error_type if error else None,
                error_message=failure.message if failure else error.message if error else None,
                page_url=page_url,
                artifact=artifact,
                healing=healing.as_dict() if healing is not None else None,
            )
        )


async def run_plan(plan: RunPlan, run_dir: Path, emit: Emit) -> None:
    """Run every case of a plan, reporting through `emit`. Always ends with `run_finished`."""
    runner = PlanRunner(plan, run_dir, emit)
    try:
        await runner.run()
    except Exception as exc:
        logger.exception("engine_run_failed")
        await emit(
            RunFinished(
                status="aborted",
                error_type="internal",
                error_message=f"The engine stopped unexpectedly ({type(exc).__name__}).",
            ).model_dump(mode="json")
        )
