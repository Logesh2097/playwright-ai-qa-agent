"""Evidence storage: where the files are kept, what is accepted, and how long.

`EvidenceStorage` is what the rest of the application uses. The one
implementation keeps files in a directory tree, one branch per organization;
an object store would implement the same five methods.

Rules that hold for any implementation:

- a storage key is generated here and never taken from a request;
- a key is only ever resolved inside the branch of the organization asking;
- a file is checked before it is kept: its kind must be known, its content must
  be what its kind says it is, and it must fit the size limits;
- nothing here produces a public address. Files leave through the API only.
"""

import contextlib
import hashlib
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from app.core.config import Settings

# kind -> (content type, file extension)
KINDS: dict[str, tuple[str, str]] = {
    "screenshot": ("image/png", ".png"),
    "video": ("video/webm", ".webm"),
    "trace": ("application/zip", ".zip"),
    "console_log": ("application/json", ".json"),
    "network_log": ("application/json", ".json"),
}
RECORDINGS = frozenset({"video", "trace"})
LOGS = frozenset({"console_log", "network_log"})
_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "video/webm": (b"\x1a\x45\xdf\xa3",),
    "application/zip": (b"PK\x03\x04", b"PK\x05\x06"),
}
CHUNK = 64 * 1024


class EvidenceRejectedError(Exception):
    """A file that will not be stored. `code` is short and safe to record."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class EvidenceLimits:
    screenshot_bytes: int
    recording_bytes: int
    log_bytes: int
    execution_bytes: int
    retention_days: int
    recording_retention_days: int

    @classmethod
    def from_settings(cls, settings: Settings) -> "EvidenceLimits":
        return cls(
            screenshot_bytes=settings.evidence_max_screenshot_bytes,
            recording_bytes=settings.evidence_max_recording_bytes,
            log_bytes=settings.evidence_max_log_bytes,
            execution_bytes=settings.evidence_max_execution_bytes,
            retention_days=settings.evidence_retention_days,
            recording_retention_days=settings.evidence_recording_retention_days,
        )

    def max_bytes(self, kind: str) -> int:
        if kind in RECORDINGS:
            return self.recording_bytes
        if kind in LOGS:
            return self.log_bytes
        return self.screenshot_bytes

    def expires_at(self, kind: str, now: datetime | None = None) -> datetime:
        """Recordings are large and show everything that happened; they are kept for less long."""
        days = self.recording_retention_days if kind in RECORDINGS else self.retention_days
        return (now or datetime.now(UTC)) + timedelta(days=days)


def validate(kind: str, content_type: str, data: bytes, limits: EvidenceLimits) -> None:
    """Raise `EvidenceRejectedError` unless the file is what it claims to be and small enough."""
    known = KINDS.get(kind)
    if known is None:
        raise EvidenceRejectedError("unknown_kind", f"Evidence of kind {kind!r} is not stored.")
    if content_type != known[0]:
        raise EvidenceRejectedError(
            "wrong_content_type", f"A {kind} must be {known[0]}, not {content_type}."
        )
    if not data:
        raise EvidenceRejectedError("empty", "The file is empty.")
    if len(data) > limits.max_bytes(kind):
        raise EvidenceRejectedError(
            "too_large", f"The file is larger than {limits.max_bytes(kind)} bytes."
        )
    signatures = _SIGNATURES.get(content_type)
    if signatures is not None:
        if not data.startswith(signatures):
            raise EvidenceRejectedError(
                "invalid_content", f"The content is not a valid {content_type} file."
            )
        return
    try:
        parsed = json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise EvidenceRejectedError("invalid_content", "The content is not valid JSON.") from exc
    if not isinstance(parsed, dict | list):
        raise EvidenceRejectedError("invalid_content", "The content is not a JSON document.")


def extension_of(content_type: str) -> str:
    return next((ext for kind_type, ext in KINDS.values() if kind_type == content_type), ".bin")


@dataclass(frozen=True, slots=True)
class StoredFile:
    key: str
    size_bytes: int
    sha256: str


class EvidenceStorage(Protocol):
    def put(
        self, organization_id: uuid.UUID, execution_id: uuid.UUID, content_type: str, data: bytes
    ) -> StoredFile: ...

    def read(self, organization_id: uuid.UUID, key: str) -> bytes | None:
        """The whole file, or None if this organization has no such file."""
        ...

    def chunks(self, organization_id: uuid.UUID, key: str) -> Iterator[bytes] | None:
        """The file piece by piece, or None if this organization has no such file."""
        ...

    def delete(self, organization_id: uuid.UUID, key: str) -> bool: ...

    def delete_execution(self, organization_id: uuid.UUID, execution_id: uuid.UUID) -> None: ...

    def organizations(self) -> list[uuid.UUID]:
        """Organizations that have any file. Used by the retention job."""
        ...


class EvidenceStore:
    """`EvidenceStorage` on a local or mounted directory."""

    def __init__(self, root: str) -> None:
        self._root = Path(root)

    def put(
        self, organization_id: uuid.UUID, execution_id: uuid.UUID, content_type: str, data: bytes
    ) -> StoredFile:
        key = f"{organization_id}/{execution_id}/{uuid.uuid4().hex}{extension_of(content_type)}"
        path = self._root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o640)
        return StoredFile(key, len(data), hashlib.sha256(data).hexdigest())

    def path(self, organization_id: uuid.UUID, key: str) -> Path | None:
        """The file for a key of this organization, or None."""
        root = self._root.resolve()
        path = (root / key).resolve()
        if not path.is_relative_to(root / str(organization_id)) or not path.is_file():
            return None
        return path

    def read(self, organization_id: uuid.UUID, key: str) -> bytes | None:
        path = self.path(organization_id, key)
        return path.read_bytes() if path is not None else None

    def chunks(self, organization_id: uuid.UUID, key: str) -> Iterator[bytes] | None:
        path = self.path(organization_id, key)
        if path is None:
            return None

        def read() -> Iterator[bytes]:
            with path.open("rb") as handle:
                while piece := handle.read(CHUNK):
                    yield piece

        return read()

    def delete(self, organization_id: uuid.UUID, key: str) -> bool:
        path = self.path(organization_id, key)
        if path is None:
            return False
        path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            path.parent.rmdir()  # only succeeds when the execution has no file left
        return True

    def delete_execution(self, organization_id: uuid.UUID, execution_id: uuid.UUID) -> None:
        directory = self._root / str(organization_id) / str(execution_id)
        if directory.is_dir():
            for item in directory.iterdir():
                item.unlink(missing_ok=True)
            directory.rmdir()

    def organizations(self) -> list[uuid.UUID]:
        found: list[uuid.UUID] = []
        if not self._root.is_dir():
            return found
        for item in self._root.iterdir():
            try:
                found.append(uuid.UUID(item.name))
            except ValueError:
                continue
        return found


def evidence_storage(settings: Settings) -> EvidenceStorage:
    """The storage this installation uses. The one place that knows which."""
    return EvidenceStore(settings.evidence_root)
