"""The GitHub REST API calls the platform makes.

Authentication is one of the two mechanisms GitHub supports for integrations:

- a **fine-grained personal access token**, limited by its owner to chosen
  repositories and permissions;
- a **GitHub App installation**: the platform signs a short-lived JWT with the
  app's private key and exchanges it for an installation token that lasts an
  hour and can only reach the repositories the app was installed on.

Either way the token goes in the `Authorization` header and nowhere else: it is
never put in a URL, a log line or an error message.

Permissions needed, and for what:

    Metadata: read          always (GitHub requires it)
    Pull requests: read     pull request details and changed files
    Contents: read          commits and branches
    Commit statuses: write  only if statuses are switched on for a repository
    Pull requests: write    only if comments are switched on for a repository
"""

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt

API_VERSION = "2022-11-28"
USER_AGENT = "qa-agent-platform"
PER_PAGE = 100
# GitHub lists at most 3000 changed files for a pull request.
MAX_FILE_PAGES = 30
MAX_LIST_PAGES = 10


class GitHubError(Exception):
    """A GitHub call that did not succeed. `code` is stable; the message is safe to show."""

    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status

    @property
    def transient(self) -> bool:
        return self.code in ("unavailable", "rate_limited", "timeout")


@dataclass(frozen=True, slots=True)
class Repository:
    id: int
    full_name: str
    default_branch: str
    private: bool
    archived: bool
    html_url: str
    description: str | None
    # What the token may do there, as GitHub reports it.
    permissions: dict[str, bool]


@dataclass(frozen=True, slots=True)
class PullRequest:
    number: int
    title: str
    state: str
    draft: bool
    author: str | None
    html_url: str
    head_sha: str
    head_ref: str
    head_repo_id: int | None
    base_ref: str
    base_repo_id: int | None
    changed_files: int | None

    @property
    def from_fork(self) -> bool:
        return self.head_repo_id != self.base_repo_id


@dataclass(frozen=True, slots=True)
class Commit:
    sha: str
    message: str
    author: str | None
    html_url: str
    committed_at: str | None


@dataclass(frozen=True, slots=True)
class ChangedFiles:
    paths: list[str]
    # False when GitHub's list may not be the whole change. Then nothing is narrowed.
    complete: bool


def repository_from(data: Mapping[str, Any]) -> Repository:
    return Repository(
        id=int(data["id"]),
        full_name=str(data["full_name"]),
        default_branch=str(data.get("default_branch") or "main"),
        private=bool(data.get("private", True)),
        archived=bool(data.get("archived", False)),
        html_url=str(data.get("html_url") or ""),
        description=data.get("description"),
        permissions={k: bool(v) for k, v in (data.get("permissions") or {}).items()},
    )


def pull_request_from(data: Mapping[str, Any]) -> PullRequest:
    head, base = data.get("head") or {}, data.get("base") or {}
    return PullRequest(
        number=int(data["number"]),
        title=str(data.get("title") or ""),
        state=str(data.get("state") or ""),
        draft=bool(data.get("draft", False)),
        author=(data.get("user") or {}).get("login"),
        html_url=str(data.get("html_url") or ""),
        head_sha=str(head["sha"]),
        head_ref=str(head.get("ref") or ""),
        head_repo_id=(head.get("repo") or {}).get("id"),
        base_ref=str(base.get("ref") or ""),
        base_repo_id=(base.get("repo") or {}).get("id"),
        changed_files=data.get("changed_files"),
    )


# --- authentication ---------------------------------------------------------------------


class TokenAuth:
    """A fine-grained personal access token (or any token GitHub accepts as a bearer)."""

    kind = "token"

    def __init__(self, token: str) -> None:
        self._token = token

    async def token(self, http: httpx.AsyncClient) -> str:
        return self._token

    def __repr__(self) -> str:
        return "TokenAuth(***)"


class AppAuth:
    """A GitHub App installation. Mints an installation token and keeps it until it nears expiry."""

    kind = "app"

    def __init__(self, app_id: str, private_key: str, installation_id: int) -> None:
        self._app_id = app_id
        self._private_key = private_key
        self._installation_id = installation_id
        self._token: str | None = None
        self._expires: datetime | None = None

    def _app_jwt(self) -> str:
        now = int(time.time())
        # Issued a minute in the past for clock drift; GitHub allows at most ten minutes.
        payload = {"iat": now - 60, "exp": now + 540, "iss": self._app_id}
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    async def token(self, http: httpx.AsyncClient) -> str:
        if (
            self._token
            and self._expires
            and self._expires > datetime.now(UTC) + timedelta(minutes=5)
        ):
            return self._token
        try:
            signed = self._app_jwt()
        except (ValueError, TypeError, jwt.PyJWTError) as exc:
            raise GitHubError(
                "invalid_credentials", "The GitHub App private key is not usable."
            ) from exc
        response = await _send(
            http,
            "POST",
            f"/app/installations/{self._installation_id}/access_tokens",
            headers=_headers(signed),
        )
        data = _json(response)
        self._token = str(data["token"])
        try:
            self._expires = datetime.fromisoformat(str(data["expires_at"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            self._expires = datetime.now(UTC) + timedelta(minutes=50)
        return self._token

    def __repr__(self) -> str:
        return f"AppAuth(app_id={self._app_id}, installation_id={self._installation_id})"


Auth = TokenAuth | AppAuth


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": USER_AGENT,
    }


async def _send(
    http: httpx.AsyncClient, method: str, path: str, *, headers: dict[str, str], **kwargs: Any
) -> httpx.Response:
    """One request. Every failure becomes a `GitHubError` that mentions no credential."""
    try:
        response = await http.request(method, path, headers=headers, **kwargs)
    except httpx.TimeoutException as exc:
        raise GitHubError("timeout", "GitHub did not answer in time.") from exc
    except httpx.HTTPError as exc:
        raise GitHubError("unavailable", "GitHub could not be reached.") from exc
    status = response.status_code
    if status < 300:
        return response
    remaining = response.headers.get("x-ratelimit-remaining")
    if status == 429 or (status == 403 and (remaining == "0" or "retry-after" in response.headers)):
        raise GitHubError(
            "rate_limited", "GitHub's rate limit was reached. Try again later.", status=status
        )
    if status == 401:
        raise GitHubError(
            "unauthorized",
            "GitHub rejected the credentials. They may have expired or been revoked.",
            status=status,
        )
    if status == 403:
        raise GitHubError(
            "forbidden",
            "The GitHub credentials do not have the permission this needs.",
            status=status,
        )
    if status == 404:
        # Also what GitHub answers for a private repository the token cannot see.
        raise GitHubError(
            "not_found",
            "GitHub has no such resource, or the credentials cannot see it.",
            status=status,
        )
    if status == 422:
        raise GitHubError("rejected", "GitHub did not accept the request.", status=status)
    if status >= 500:
        raise GitHubError(
            "unavailable", f"GitHub answered with an error ({status}).", status=status
        )
    raise GitHubError("unexpected", f"GitHub answered {status}.", status=status)


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise GitHubError("invalid_response", "GitHub's answer could not be read.") from exc


class GitHubClient:
    """Calls for one connection. Paths are built from validated repository names only."""

    def __init__(
        self,
        auth: Auth,
        *,
        base_url: str = "https://api.github.com",
        timeout_s: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._auth = auth
        # Redirects are not followed: a token is never sent to another host.
        self._http = httpx.AsyncClient(
            base_url=base_url, timeout=timeout_s, transport=transport, follow_redirects=False
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "GitHubClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        token = await self._auth.token(self._http)
        return await _send(self._http, method, path, headers=_headers(token), **kwargs)

    async def _get(self, path: str, **params: Any) -> Any:
        return _json(await self._request("GET", path, params=params or None))

    async def _pages(
        self, path: str, *, max_pages: int, key: str | None = None
    ) -> tuple[list[Any], bool]:
        """Items of a paged list, and whether the last page was reached."""
        items: list[Any] = []
        for page in range(1, max_pages + 1):
            data = await self._get(path, per_page=PER_PAGE, page=page)
            batch = data.get(key, []) if key and isinstance(data, dict) else data
            if not isinstance(batch, list):
                raise GitHubError("invalid_response", "GitHub's answer could not be read.")
            items.extend(batch)
            if len(batch) < PER_PAGE:
                return items, True
        return items, False

    # --- who am I, what can I see ---------------------------------------------------------

    async def account(self) -> str:
        """The account the credentials belong to. Also proves that they work."""
        if isinstance(self._auth, AppAuth):
            data, _ = await self._pages(
                "/installation/repositories", max_pages=1, key="repositories"
            )
            owner = (data[0].get("owner") or {}).get("login") if data else None
            return str(owner or "GitHub App installation")
        return str((await self._get("/user"))["login"])

    async def repositories(self) -> list[Repository]:
        """Repositories the credentials can reach."""
        if isinstance(self._auth, AppAuth):
            data, _ = await self._pages(
                "/installation/repositories", max_pages=MAX_LIST_PAGES, key="repositories"
            )
        else:
            data, _ = await self._pages("/user/repos", max_pages=MAX_LIST_PAGES)
        return [repository_from(item) for item in data]

    async def repository(self, full_name: str) -> Repository:
        return repository_from(await self._get(f"/repos/{full_name}"))

    async def branches(self, full_name: str) -> list[dict[str, Any]]:
        data, _ = await self._pages(f"/repos/{full_name}/branches", max_pages=MAX_LIST_PAGES)
        return [
            {
                "name": str(item["name"]),
                "sha": (item.get("commit") or {}).get("sha"),
                "protected": bool(item.get("protected", False)),
            }
            for item in data
        ]

    async def pull_request(self, full_name: str, number: int) -> PullRequest:
        return pull_request_from(await self._get(f"/repos/{full_name}/pulls/{number}"))

    async def pull_request_files(self, full_name: str, number: int) -> ChangedFiles:
        data, complete = await self._pages(
            f"/repos/{full_name}/pulls/{number}/files", max_pages=MAX_FILE_PAGES
        )
        paths: list[str] = []
        for item in data:
            paths.append(str(item["filename"]))
            # A renamed file concerns both where it was and where it is.
            if item.get("previous_filename"):
                paths.append(str(item["previous_filename"]))
        return ChangedFiles(list(dict.fromkeys(paths)), complete)

    async def compare(self, full_name: str, base: str, head: str) -> ChangedFiles:
        """Files changed between two commits, for pushes."""
        data = await self._get(f"/repos/{full_name}/compare/{base}...{head}", per_page=PER_PAGE)
        files = data.get("files") or []
        paths = [str(item["filename"]) for item in files]
        paths += [str(item["previous_filename"]) for item in files if item.get("previous_filename")]
        # The comparison lists at most 300 files and has no reliable "that was all" marker.
        complete = len(files) < 300 and int(data.get("total_commits", 0)) <= 250
        return ChangedFiles(list(dict.fromkeys(paths)), complete)

    async def commit(self, full_name: str, sha: str) -> Commit:
        data = await self._get(f"/repos/{full_name}/commits/{sha}")
        detail = data.get("commit") or {}
        return Commit(
            sha=str(data["sha"]),
            message=str(detail.get("message") or ""),
            author=(data.get("author") or {}).get("login")
            or (detail.get("author") or {}).get("name"),
            html_url=str(data.get("html_url") or ""),
            committed_at=(detail.get("committer") or {}).get("date"),
        )

    # --- writing back (only when a repository's settings allow it) -------------------------

    async def set_status(
        self,
        full_name: str,
        sha: str,
        *,
        state: str,
        context: str,
        description: str,
        target_url: str | None,
    ) -> None:
        body = {"state": state, "context": context, "description": description[:140]}
        if target_url:
            body["target_url"] = target_url
        await self._request("POST", f"/repos/{full_name}/statuses/{sha}", json=body)

    async def create_comment(self, full_name: str, number: int, body: str) -> int:
        response = await self._request(
            "POST", f"/repos/{full_name}/issues/{number}/comments", json={"body": body}
        )
        return int(_json(response)["id"])

    async def update_comment(self, full_name: str, comment_id: int, body: str) -> None:
        await self._request(
            "PATCH", f"/repos/{full_name}/issues/comments/{comment_id}", json={"body": body}
        )


def new_webhook_secret() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex
