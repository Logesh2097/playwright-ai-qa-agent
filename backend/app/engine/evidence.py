"""Evidence collection: screenshots, video and traces of one case attempt.

Files are written to the run's directory under names the engine chooses and
are reported by reference. The worker fetches and stores them; the engine keeps
nothing after the run is deleted.
"""

import os
import shutil
from pathlib import Path

from playwright.async_api import BrowserContext, Page
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator as Element

from app.engine.protocol import ArtifactKind, ArtifactRef, EvidenceSettings

MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
_TYPES: dict[str, str] = {".png": "image/png", ".webm": "video/webm", ".zip": "application/zip"}


class Evidence:
    def __init__(
        self,
        run_dir: Path,
        settings: EvidenceSettings,
        *,
        tag: str,
        allow_recordings: bool,
    ) -> None:
        self._dir = run_dir
        self._settings = settings
        self._tag = tag
        self._sequence = 0
        # Recordings would capture a secret typed by the case itself; see runner.py.
        self._recordings = allow_recordings
        self._tracing = False
        self.notes: list[str] = []
        # Elements that hold a secret: painted over in every screenshot.
        self.masked: list[Element] = []

    @property
    def video_dir(self) -> str | None:
        if self._settings.video == "off" or not self._recordings:
            return None
        path = self._dir / f"{self._tag}-video"
        path.mkdir(exist_ok=True)
        return str(path)

    def _ref(self, path: Path, kind: ArtifactKind) -> ArtifactRef | None:
        size = path.stat().st_size
        if size > MAX_ARTIFACT_BYTES:
            path.unlink(missing_ok=True)
            self.notes.append(f"{kind}_too_large")
            return None
        return ArtifactRef(
            name=path.name, kind=kind, content_type=_TYPES[path.suffix], size_bytes=size
        )

    async def screenshot(
        self, page: Page, label: str, *, full_page: bool | None = None
    ) -> ArtifactRef | None:
        self._sequence += 1
        path = self._dir / f"{self._tag}-{self._sequence:03d}-{label}.png"
        try:
            await page.screenshot(
                path=str(path),
                full_page=self._settings.full_page if full_page is None else full_page,
                mask=self.masked,
                timeout=5_000,
            )
        except PlaywrightError:
            self.notes.append("screenshot_failed")
            return None
        return self._ref(path, "screenshot")

    async def start_trace(self, context: BrowserContext) -> None:
        if self._settings.trace == "off" or not self._recordings:
            return
        await context.tracing.start(screenshots=True, snapshots=True)
        self._tracing = True

    async def stop_trace(self, context: BrowserContext, *, passed: bool) -> ArtifactRef | None:
        if not self._tracing:
            return None
        self._tracing = False
        keep = self._settings.trace == "always" or not passed
        path = self._dir / f"{self._tag}-trace.zip"
        try:
            await context.tracing.stop(path=str(path) if keep else None)
        except PlaywrightError:
            self.notes.append("trace_failed")
            return None
        return self._ref(path, "trace") if keep else None

    async def collect_video(self, page: Page | None, *, passed: bool) -> ArtifactRef | None:
        """Call after the context is closed: the file is only complete then."""
        if page is None or page.video is None:
            return None
        try:
            source = Path(await page.video.path())
        except PlaywrightError:
            return None
        # Small local file operations on the run's own directory.
        if not source.exists():  # noqa: ASYNC240
            return None
        if self._settings.video != "always" and passed:
            shutil.rmtree(source.parent, ignore_errors=True)
            return None
        path = self._dir / f"{self._tag}-video.webm"
        os.replace(source, path)
        shutil.rmtree(source.parent, ignore_errors=True)
        return self._ref(path, "video")
