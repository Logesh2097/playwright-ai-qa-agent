"""GitHub webhooks: verifying that a delivery is GitHub's, and reading what it says.

A delivery is only looked at after its signature has been checked against the
secret of the repository link it was sent to. The body is used exactly as
received: the signature covers those bytes and no others.
"""

import fnmatch
import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from typing import Any

SIGNATURE_HEADER = "x-hub-signature-256"
DELIVERY_HEADER = "x-github-delivery"
EVENT_HEADER = "x-github-event"
_SIGNATURE = re.compile(r"^sha256=[0-9a-f]{64}$")
_DELIVERY = re.compile(r"^[A-Za-z0-9-]{8,80}$")
_EVENT = re.compile(r"^[a-z_]{1,40}$")
_SHA = re.compile(r"^[0-9a-f]{40,64}$")
REPOSITORY_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
ZERO_SHA = "0" * 40
# Pull request actions after which the code to test has changed, or testing begins.
PR_ACTIONS = frozenset({"opened", "synchronize", "reopened", "ready_for_review"})


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """True only for a well-formed SHA-256 signature of exactly these bytes.

    The older SHA-1 header is not accepted. The comparison takes the same time
    whatever the first differing character is.
    """
    if not secret or header is None or not _SIGNATURE.match(header):
        return False
    return hmac.compare_digest(sign(secret, body), header)


def valid_delivery_id(value: str | None) -> bool:
    return value is not None and bool(_DELIVERY.match(value))


def valid_event(value: str | None) -> bool:
    return value is not None and bool(_EVENT.match(value))


def branch_matches(branch: str, patterns: list[str]) -> bool:
    """Exact names or shell-style patterns (`release/*`). No pattern, no match."""
    return any(branch == pattern or fnmatch.fnmatchcase(branch, pattern) for pattern in patterns)


@dataclass(frozen=True, slots=True)
class Event:
    """What a delivery asks for, or why it asks for nothing."""

    kind: str  # "pull_request", "push", "ping" or "ignored"
    reason: str | None = None
    repository_id: int | None = None
    action: str | None = None
    subject: dict[str, Any] = field(default_factory=dict)

    @property
    def triggers(self) -> bool:
        return self.kind in ("pull_request", "push")


def _ignored(reason: str, **more: Any) -> Event:
    return Event("ignored", reason, **more)


def parse_event(event: str, body: bytes) -> Event:
    """Read a verified delivery. Anything unexpected is ignored, never guessed at."""
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return _ignored("The payload is not JSON.")
    if not isinstance(payload, dict):
        return _ignored("The payload is not a JSON object.")
    repository = payload.get("repository")
    repository_id = repository.get("id") if isinstance(repository, dict) else None
    if not isinstance(repository_id, int):
        repository_id = None

    if event == "ping":
        return Event("ping", repository_id=repository_id)

    if event == "pull_request":
        action = payload.get("action")
        pull = payload.get("pull_request")
        if not isinstance(action, str) or not isinstance(pull, dict):
            return _ignored("The pull request event is incomplete.", repository_id=repository_id)
        head, base = pull.get("head") or {}, pull.get("base") or {}
        number, sha = pull.get("number"), head.get("sha")
        if not isinstance(number, int) or not isinstance(sha, str) or not _SHA.match(sha):
            return _ignored("The pull request event is incomplete.", repository_id=repository_id)
        subject = {
            "pull_request": number,
            "head_sha": sha,
            "head_ref": str(head.get("ref") or ""),
            "base_ref": str(base.get("ref") or ""),
            "draft": bool(pull.get("draft", False)),
            "from_fork": (head.get("repo") or {}).get("id") != (base.get("repo") or {}).get("id"),
            "title": str(pull.get("title") or "")[:200],
            "author": (pull.get("user") or {}).get("login"),
        }
        if action not in PR_ACTIONS:
            return Event(
                "ignored", f"Pull request action '{action[:40]}' does not start tests.",
                repository_id, action[:40], subject,
            )  # fmt: skip
        if pull.get("state") != "open":
            return Event("ignored", "The pull request is not open.", repository_id, action, subject)
        return Event("pull_request", None, repository_id, action, subject)

    if event == "push":
        ref, after = payload.get("ref"), payload.get("after")
        if not isinstance(ref, str) or not isinstance(after, str) or not _SHA.match(after):
            return _ignored("The push event is incomplete.", repository_id=repository_id)
        subject = {
            "ref": ref[:300],
            "head_sha": after,
            "before": payload.get("before") if isinstance(payload.get("before"), str) else None,
            "pusher": (payload.get("pusher") or {}).get("name"),
        }
        if not ref.startswith("refs/heads/"):
            return Event(
                "ignored", "Only pushes to branches start tests.", repository_id, None, subject
            )
        if payload.get("deleted") or after == ZERO_SHA:
            return Event("ignored", "The branch was deleted.", repository_id, None, subject)
        subject["branch"] = ref.removeprefix("refs/heads/")
        return Event("push", None, repository_id, None, subject)

    return _ignored(f"Event '{event}' is not used.", repository_id=repository_id)
