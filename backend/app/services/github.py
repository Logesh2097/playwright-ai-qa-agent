"""GitHub for a project: connections, linked repositories, webhooks, and writing results back.

What may happen is decided by rows a person created, never by what arrives:

- a **connection** (`Integration`, provider `github`) holds credentials an
  organization admin entered, encrypted like any other secret;
- a **linked repository** (`GithubRepository`) says that one project uses one
  repository, and what is allowed there: which events start tests, for which
  branches, in which environment, with which suites, and whether a status or a
  comment is written back. Everything is off until someone switches it on.

A webhook delivery is trusted only as far as its signature goes: it shows that
GitHub sent it for that repository. It selects nothing itself. The suites,
the environment and the branches come from the link.
"""

import json
import logging
import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import (
    AppError,
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
    UnauthenticatedError,
    ValidationFailedError,
)
from app.core.jobs import JobQueue
from app.core.redis import RedisClient
from app.core.security.crypto import CredentialCipher, DecryptionError, EncryptedSecret
from app.core.security.permissions import Permission
from app.db.context import set_current_org
from app.integrations.github import webhooks
from app.integrations.github.client import (
    AppAuth,
    Auth,
    ChangedFiles,
    GitHubClient,
    GitHubError,
    Repository,
    TokenAuth,
    new_webhook_secret,
)
from app.integrations.github.reporting import (
    RunSummary,
    commit_status,
    pr_comment,
    status_context,
)
from app.integrations.github.selection import Selection, parse_rules
from app.integrations.github.selection import select as select_suites
from app.models import GithubDelivery, GithubRepository, Integration, Project, TestExecution
from app.reporting.builder import build_execution_report
from app.reporting.ci import ci_outcome
from app.repositories.executions import ExecutionRepository
from app.repositories.projects import ApplicationRepository, EnvironmentRepository
from app.repositories.test_cases import SuiteRepository
from app.schemas.executions import ExecutionCreate
from app.services.audit import AuditEvent, AuditService
from app.services.executions import Automation, ExecutionService
from app.services.organizations import OrgContext
from app.services.projects import ProjectContext

logger = logging.getLogger(__name__)

JOB_PROCESS = "process_github_delivery"
MAX_SUITES = 20
MAX_RULES = 100


@dataclass(frozen=True, slots=True)
class LinkCreated:
    repository: GithubRepository
    # Shown once, to be entered in the repository's webhook settings on GitHub.
    webhook_secret: str


@dataclass(frozen=True, slots=True)
class Received:
    status: str  # accepted, ignored, duplicate, pong
    reason: str | None = None
    delivery_id: uuid.UUID | None = None


def _upstream(exc: GitHubError) -> AppError:
    """A GitHub failure as an answer of ours. Never carries GitHub's own text or a credential."""
    if exc.code in ("unauthorized", "forbidden", "invalid_credentials"):
        return ConflictError(str(exc), code=f"github_{exc.code}")
    if exc.code == "not_found":
        return NotFoundError(str(exc), code="github_not_found")
    if exc.code == "rejected":
        return ConflictError(str(exc), code="github_rejected")
    return ServiceUnavailableError(str(exc), code=f"github_{exc.code}")


class GithubClients:
    """Builds API clients from stored connections. The one place credentials are decrypted."""

    def __init__(
        self,
        settings: Settings,
        cipher: CredentialCipher,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._cipher = cipher
        self._transport = transport

    def with_auth(self, auth: Auth) -> GitHubClient:
        return GitHubClient(
            auth,
            base_url=self._settings.github_api_url,
            timeout_s=self._settings.github_timeout_s,
            transport=self._transport,
        )

    def seal(
        self, payload: dict[str, str], organization_id: uuid.UUID, row_id: uuid.UUID
    ) -> EncryptedSecret:
        return self._cipher.encrypt(payload, organization_id=organization_id, credential_id=row_id)

    def open(self, row: Integration | GithubRepository) -> dict[str, str]:
        if row.secret_ciphertext is None or row.nonce is None or row.wrapped_dek is None:
            raise ConflictError("This connection has no credentials.", code="github_not_connected")
        try:
            return self._cipher.decrypt(
                EncryptedSecret(
                    row.secret_ciphertext, row.nonce, row.wrapped_dek, row.key_version or 0
                ),
                organization_id=row.organization_id,
                credential_id=row.id,
            )
        except DecryptionError as exc:
            raise ConflictError(
                "The stored GitHub credentials cannot be read. Enter them again.",
                code="github_credentials_unreadable",
            ) from exc

    def for_connection(self, connection: Integration) -> GitHubClient:
        secret = self.open(connection)
        if connection.config.get("auth") == "app":
            auth: Auth = AppAuth(
                str(connection.config.get("app_id", "")),
                secret["private_key"],
                int(connection.installation_id or 0),
            )
        else:
            auth = TokenAuth(secret["token"])
        return self.with_auth(auth)


def _store(row: Integration | GithubRepository, sealed: EncryptedSecret) -> None:
    row.secret_ciphertext = sealed.ciphertext
    row.nonce = sealed.nonce
    row.wrapped_dek = sealed.wrapped_dek
    row.key_version = sealed.key_version


class GithubService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        audit: AuditService,
        jobs: JobQueue,
        redis: RedisClient,
        settings: Settings,
        clients: GithubClients,
    ) -> None:
        self._session = session
        self._audit = audit
        self._jobs = jobs
        self._redis = redis
        self._settings = settings
        self._clients = clients

    async def _record(
        self,
        organization_id: uuid.UUID,
        actor_id: uuid.UUID | None,
        action: str,
        resource_type: str,
        resource_id: uuid.UUID,
        changes: dict[str, Any],
        actor_type: str = "user",
    ) -> None:
        await self._audit.record(
            self._session,
            AuditEvent(
                action=action,
                actor_id=actor_id,
                actor_type=actor_type,
                organization_id=organization_id,
                resource_type=resource_type,
                resource_id=resource_id,
                changes=changes,
            ),
        )

    # --- connections ---------------------------------------------------------------------

    async def connections(self, org: OrgContext) -> Sequence[Integration]:
        org.require(Permission.INTEGRATIONS_MANAGE)
        result = await self._session.execute(
            select(Integration)
            .where(
                Integration.organization_id == org.organization.id, Integration.provider == "github"
            )
            .order_by(Integration.created_at)
        )
        return result.scalars().all()

    async def connection(self, organization_id: uuid.UUID, connection_id: uuid.UUID) -> Integration:
        row = await self._session.get(Integration, connection_id)
        if row is None or row.organization_id != organization_id or row.provider != "github":
            raise NotFoundError("GitHub connection not found.")
        return row

    async def connect(
        self,
        org: OrgContext,
        *,
        name: str,
        token: str | None,
        app_id: str | None,
        private_key: str | None,
        installation_id: int | None,
    ) -> Integration:
        """Store credentials after proving, by one call to GitHub, that they work."""
        org.require(Permission.INTEGRATIONS_MANAGE)
        if token is not None:
            auth: Auth = TokenAuth(token)
            secret, kind = {"token": token}, "token"
        else:
            auth = AppAuth(str(app_id), str(private_key), int(installation_id or 0))
            secret, kind = {"private_key": str(private_key)}, "app"
        try:
            async with self._clients.with_auth(auth) as client:
                account = await client.account()
        except GitHubError as exc:
            raise _upstream(exc) from exc
        row = Integration(
            organization_id=org.organization.id,
            provider="github",
            status="active",
            config={
                "name": name,
                "auth": kind,
                **({"app_id": str(app_id)} if kind == "app" else {}),
            },
            external_account=account,
            installation_id=installation_id if kind == "app" else None,
            created_by=org.user.id,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise ConflictError(
                "This GitHub App installation is already connected.",
                code="github_already_connected",
            ) from exc
        _store(row, self._clients.seal(secret, org.organization.id, row.id))
        await self._record(
            org.organization.id, org.user.id, "github.connect", "integration", row.id,
            {"name": name, "auth": kind, "account": account},
        )  # fmt: skip
        return row

    async def disconnect(self, org: OrgContext, connection_id: uuid.UUID) -> None:
        """Remove the credentials and every repository link that used them."""
        org.require(Permission.INTEGRATIONS_MANAGE)
        row = await self.connection(org.organization.id, connection_id)
        await self._record(
            org.organization.id, org.user.id, "github.disconnect", "integration", row.id,
            {"name": row.config.get("name"), "account": row.external_account},
        )  # fmt: skip
        await self._session.delete(row)
        await self._session.flush()

    async def available_repositories(
        self, org: OrgContext, connection_id: uuid.UUID
    ) -> list[Repository]:
        """Repositories the stored credentials can reach: what there is to choose from."""
        org.require(Permission.INTEGRATIONS_MANAGE)
        row = await self.connection(org.organization.id, connection_id)
        try:
            async with self._clients.for_connection(row) as client:
                found = await client.repositories()
        except GitHubError as exc:
            await self._mark(row, exc)
            raise _upstream(exc) from exc
        return found

    async def _mark(self, connection: Integration, exc: GitHubError) -> None:
        if exc.code in ("unauthorized", "invalid_credentials"):
            connection.status = "error"

    # --- linked repositories -------------------------------------------------------------

    async def links(self, ctx: ProjectContext) -> Sequence[GithubRepository]:
        ctx.require(Permission.PROJECTS_READ)
        result = await self._session.execute(
            select(GithubRepository)
            .where(
                GithubRepository.organization_id == ctx.organization_id,
                GithubRepository.project_id == ctx.project.id,
            )
            .order_by(GithubRepository.full_name)
        )
        return result.scalars().all()

    async def link_of(self, ctx: ProjectContext, link_id: uuid.UUID) -> GithubRepository:
        ctx.require(Permission.PROJECTS_READ)
        row = await self._session.get(GithubRepository, link_id)
        if (
            row is None
            or row.organization_id != ctx.organization_id
            or row.project_id != ctx.project.id
        ):
            raise NotFoundError("Linked repository not found.")
        return row

    async def link(
        self, ctx: ProjectContext, *, connection_id: uuid.UUID, full_name: str
    ) -> LinkCreated:
        """Link one repository to the project. Nothing is triggered until that is switched on."""
        ctx.require(Permission.INTEGRATIONS_MANAGE)
        ctx.require_writable()
        if not webhooks.REPOSITORY_NAME.match(full_name):
            raise ValidationFailedError("Give the repository as owner/name.")
        connection = await self.connection(ctx.organization_id, connection_id)
        try:
            async with self._clients.for_connection(connection) as client:
                # Proves the credentials can see this repository, and gets its permanent ID.
                repository = await client.repository(full_name)
        except GitHubError as exc:
            await self._mark(connection, exc)
            raise _upstream(exc) from exc
        if repository.archived:
            raise ConflictError(
                "This repository is archived on GitHub.", code="github_repository_archived"
            )
        secret = new_webhook_secret()
        row = GithubRepository(
            organization_id=ctx.organization_id,
            project_id=ctx.project.id,
            integration_id=connection.id,
            github_repo_id=repository.id,
            full_name=repository.full_name,
            default_branch=repository.default_branch,
            is_private=repository.private,
            hook_token=secrets.token_urlsafe(24),
            secret_ciphertext=b"",
            nonce=b"",
            wrapped_dek=b"",
            key_version=0,
            branches=[repository.default_branch],
            created_by=ctx.user.id,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise ConflictError(
                "This repository is already linked to the project.", code="github_already_linked"
            ) from exc
        _store(row, self._clients.seal({"webhook_secret": secret}, ctx.organization_id, row.id))
        await self._record(
            ctx.organization_id, ctx.user.id, "github.repository.link", "github_repository", row.id,
            {"repository": row.full_name, "github_repo_id": row.github_repo_id},
        )  # fmt: skip
        return LinkCreated(row, secret)

    async def unlink(self, ctx: ProjectContext, link_id: uuid.UUID) -> None:
        ctx.require(Permission.INTEGRATIONS_MANAGE)
        row = await self.link_of(ctx, link_id)
        await self._record(
            ctx.organization_id, ctx.user.id, "github.repository.unlink", "github_repository", row.id,
            {"repository": row.full_name},
        )  # fmt: skip
        await self._session.delete(row)
        await self._session.flush()

    async def rotate_secret(self, ctx: ProjectContext, link_id: uuid.UUID) -> LinkCreated:
        """A new webhook secret. Deliveries signed with the old one are refused from now on."""
        ctx.require(Permission.INTEGRATIONS_MANAGE)
        row = await self.link_of(ctx, link_id)
        secret = new_webhook_secret()
        _store(row, self._clients.seal({"webhook_secret": secret}, ctx.organization_id, row.id))
        await self._record(
            ctx.organization_id, ctx.user.id, "github.repository.rotate_secret", "github_repository",
            row.id, {"repository": row.full_name},
        )  # fmt: skip
        return LinkCreated(row, secret)

    async def configure(
        self, ctx: ProjectContext, link_id: uuid.UUID, changes: dict[str, Any]
    ) -> GithubRepository:
        """Change what the link allows. Every reference is checked to belong to this project."""
        ctx.require(Permission.INTEGRATIONS_MANAGE)
        ctx.require_writable()
        row = await self.link_of(ctx, link_id)
        if "environment_id" in changes and changes["environment_id"] is not None:
            await self._environment_in_project(ctx, changes["environment_id"])
        suite_ids = (
            [str(s) for s in changes["suite_ids"]]
            if "suite_ids" in changes
            else list(row.suite_ids)
        )
        if "suite_ids" in changes:
            suites = SuiteRepository(self._session, ctx.organization_id)
            for suite_id in dict.fromkeys(suite_ids):
                if await suites.get_in_project(ctx.project.id, uuid.UUID(suite_id)) is None:
                    raise NotFoundError("Test suite not found.")
            changes["suite_ids"] = list(dict.fromkeys(suite_ids))
        rules = changes.get("path_rules", row.path_rules)
        for rule in rules:
            unknown = [s for s in rule["suite_ids"] if str(s) not in suite_ids]
            if unknown:
                raise ValidationFailedError(
                    f"The rule for '{rule['pattern']}' names a suite that is not in this repository's set."
                )
        if "path_rules" in changes:
            changes["path_rules"] = [
                {"pattern": r["pattern"], "suite_ids": [str(s) for s in r["suite_ids"]]}
                for r in rules
            ]
        for name, value in changes.items():
            setattr(row, name, value)
        triggers = row.on_pull_request or row.on_push
        if triggers and (row.environment_id is None or not row.suite_ids or not row.branches):
            raise ValidationFailedError(
                "Before tests can be triggered, choose an environment, at least one suite and "
                "at least one branch."
            )
        await self._session.flush()
        await self._record(
            ctx.organization_id, ctx.user.id, "github.repository.configure", "github_repository", row.id,
            {"repository": row.full_name, "changed": sorted(changes)},
        )  # fmt: skip
        return row

    async def _environment_in_project(self, ctx: ProjectContext, environment_id: uuid.UUID) -> None:
        environment = await EnvironmentRepository(self._session, ctx.organization_id).get(
            environment_id
        )
        application = (
            await ApplicationRepository(self._session, ctx.organization_id).get(
                environment.application_id
            )
            if environment
            else None
        )
        if environment is None or application is None or application.project_id != ctx.project.id:
            raise NotFoundError("Environment not found.")

    def webhook_path(self, row: GithubRepository) -> str:
        return f"/api/v1/webhooks/github/{row.organization_id}/{row.hook_token}"

    async def deliveries(self, ctx: ProjectContext, link_id: uuid.UUID) -> Sequence[GithubDelivery]:
        row = await self.link_of(ctx, link_id)
        result = await self._session.execute(
            select(GithubDelivery)
            .where(
                GithubDelivery.organization_id == ctx.organization_id,
                GithubDelivery.repository_id == row.id,
            )
            .order_by(GithubDelivery.created_at.desc())
            .limit(50)
        )
        return result.scalars().all()

    # --- reading from GitHub, for a linked repository only -------------------------------

    async def _client(self, row: GithubRepository) -> GitHubClient:
        connection = await self.connection(row.organization_id, row.integration_id)
        return self._clients.for_connection(connection)

    async def _checked(self, client: GitHubClient, row: GithubRepository) -> Repository:
        """The repository as GitHub has it now. It must still be the one that was linked."""
        repository = await client.repository(row.full_name)
        if repository.id != row.github_repo_id:
            # The name now belongs to a different repository (renamed, deleted and re-created).
            raise GitHubError(
                "not_found", "The linked repository is no longer at this name on GitHub."
            )
        return repository

    async def metadata(self, ctx: ProjectContext, link_id: uuid.UUID) -> dict[str, Any]:
        row = await self.link_of(ctx, link_id)
        try:
            async with await self._client(row) as client:
                repository = await self._checked(client, row)
                branches = await client.branches(row.full_name)
        except GitHubError as exc:
            raise _upstream(exc) from exc
        row.default_branch, row.is_private = repository.default_branch, repository.private
        return {"repository": repository, "branches": branches}

    async def pull_request(
        self, ctx: ProjectContext, link_id: uuid.UUID, number: int
    ) -> dict[str, Any]:
        row = await self.link_of(ctx, link_id)
        try:
            async with await self._client(row) as client:
                await self._checked(client, row)
                pull = await client.pull_request(row.full_name, number)
                files = await client.pull_request_files(row.full_name, number)
        except GitHubError as exc:
            raise _upstream(exc) from exc
        return {
            "pull_request": pull,
            "files": files,
            "selection": self.suggest(row, files.paths, files.complete),
        }

    async def commit(self, ctx: ProjectContext, link_id: uuid.UUID, sha: str) -> Any:
        row = await self.link_of(ctx, link_id)
        try:
            async with await self._client(row) as client:
                await self._checked(client, row)
                return await client.commit(row.full_name, sha)
        except GitHubError as exc:
            raise _upstream(exc) from exc

    @staticmethod
    def suggest(row: GithubRepository, files: Sequence[str] | None, complete: bool) -> Selection:
        """Which suites a change concerns, by the link's path rules."""
        return select_suites(
            mode=row.selection_mode,
            full_suite_ids=[str(s) for s in row.suite_ids],
            rules=parse_rules(row.path_rules),
            changed_files=files,
            complete=complete,
        )

    # --- receiving a webhook --------------------------------------------------------------

    async def receive(
        self,
        *,
        organization_id: uuid.UUID,
        hook_token: str,
        signature: str | None,
        delivery_id: str | None,
        event: str | None,
        body: bytes,
    ) -> Received:
        """Verify one delivery, record it once, and queue it if the link allows what it asks."""
        await set_current_org(self._session, organization_id)
        result = await self._session.execute(
            select(GithubRepository).where(
                GithubRepository.organization_id == organization_id,
                GithubRepository.hook_token == hook_token,
            )
        )
        row = result.scalar_one_or_none()
        refused = UnauthenticatedError(
            "The webhook signature is not valid.", code="invalid_signature"
        )
        if row is None:
            # The same answer as for a wrong signature: an address is not confirmed either way.
            raise refused
        try:
            secret = self._clients.open(row)["webhook_secret"]
        except (AppError, KeyError) as exc:
            raise refused from exc
        if not webhooks.verify_signature(secret, body, signature):
            raise refused
        if not webhooks.valid_delivery_id(delivery_id) or not webhooks.valid_event(event):
            raise ValidationFailedError("The delivery headers are missing or malformed.")
        assert delivery_id is not None and event is not None  # noqa: S101 - checked above

        parsed = webhooks.parse_event(event, body)
        if parsed.kind == "ping":
            return Received("pong")
        outcome, reason = self._decide(row, parsed)
        delivery = GithubDelivery(
            organization_id=organization_id,
            repository_id=row.id,
            delivery_id=delivery_id,
            event=event,
            action=parsed.action,
            outcome=outcome,
            reason=reason,
            subject=parsed.subject,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(delivery)
                await self._session.flush()
        except IntegrityError:
            # GitHub redelivers with the same delivery ID. It was handled the first time.
            return Received("duplicate", "This delivery was already received.")
        if outcome != "accepted":
            return Received("ignored", reason, delivery.id)
        await self._jobs.enqueue(
            JOB_PROCESS, organization_id=str(organization_id), delivery_id=str(delivery.id)
        )
        return Received("accepted", None, delivery.id)

    @staticmethod
    def _decide(row: GithubRepository, event: webhooks.Event) -> tuple[str, str | None]:
        """Whether a verified event may start tests. Only the link's settings say yes."""
        if not event.triggers:
            return "ignored", event.reason
        if event.repository_id != row.github_repo_id:
            return "ignored", "The event is for a different repository than the one linked."
        if not row.enabled:
            return "ignored", "The link is switched off."
        subject = event.subject
        if event.kind == "pull_request":
            if not row.on_pull_request:
                return "ignored", "Pull request testing is not switched on for this repository."
            if subject.get("draft"):
                return "ignored", "Draft pull requests are not tested."
            branch = str(subject.get("base_ref", ""))
        else:
            if not row.on_push:
                return "ignored", "Testing on push is not switched on for this repository."
            branch = str(subject.get("branch", ""))
        if not webhooks.branch_matches(branch, list(row.branches)):
            return "ignored", f"Branch '{branch[:100]}' is not one of the configured branches."
        if row.environment_id is None or not row.suite_ids:
            return "ignored", "No environment or suite is configured for this repository."
        return "accepted", None


# --- worker side ---------------------------------------------------------------------------


class _ArqQueue:
    """The worker's own queue connection, as a `JobQueue`."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def enqueue(self, job: str, **kwargs: Any) -> None:
        await self._pool.enqueue_job(job, **kwargs)


class GithubWorker:
    """What happens after a delivery was accepted, and after an execution it started ends."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        redis: RedisClient,
        queue: Any,
        clients: GithubClients,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._redis = redis
        self._jobs: JobQueue = queue if hasattr(queue, "enqueue") else _ArqQueue(queue)
        self._clients = clients

    def _report_url(self, execution: TestExecution) -> str:
        base = self._settings.public_base_url.rstrip("/")
        return f"{base}/projects/{execution.project_id}/executions/{execution.id}"

    async def process(self, *, organization_id: uuid.UUID, delivery_id: uuid.UUID) -> str:
        """Read what changed, choose the suites, start the executions, report them as pending."""
        async with self._session_factory() as session, session.begin():
            await set_current_org(session, organization_id)
            delivery = await session.get(GithubDelivery, delivery_id, with_for_update=True)
            if delivery is None or delivery.outcome != "accepted":
                return "nothing_to_do"
            row = await session.get(GithubRepository, delivery.repository_id)
            connection = await session.get(Integration, row.integration_id) if row else None
            project = await session.get(Project, row.project_id) if row else None
            if row is None or connection is None or project is None or not row.enabled:
                delivery.outcome, delivery.reason = (
                    "failed",
                    "The link no longer exists or is switched off.",
                )
                return "failed"
            subject = dict(delivery.subject)
            head_sha = str(subject["head_sha"])
            number = subject.get("pull_request")

            files: ChangedFiles | None = None
            notes: list[str] = []
            try:
                async with self._clients.for_connection(connection) as client:
                    if number is not None:
                        pull = await client.pull_request(row.full_name, int(number))
                        # The API is the authority on which repository this pull request is in.
                        if pull.base_repo_id != row.github_repo_id:
                            delivery.outcome = "failed"
                            delivery.reason = (
                                "GitHub says this pull request belongs to another repository."
                            )
                            return "failed"
                        if pull.head_sha != head_sha:
                            delivery.outcome = "ignored"
                            delivery.reason = (
                                "A newer commit has been pushed; its own event is tested."
                            )
                            return "superseded"
                        files = await client.pull_request_files(row.full_name, int(number))
                    elif subject.get("before") and subject["before"] != webhooks.ZERO_SHA:
                        files = await client.compare(
                            row.full_name, str(subject["before"]), head_sha
                        )
            except GitHubError as exc:
                # The change cannot be read: nothing is narrowed, and the tests still run.
                notes.append(f"GitHub could not be asked about the change ({exc.code}).")
                files = None

            selection = GithubService.suggest(
                row, files.paths if files else None, bool(files and files.complete)
            )
            service = ExecutionService(
                session=session,
                audit=AuditService(self._session_factory),
                jobs=self._jobs,
                redis=self._redis,
                settings=self._settings,
            )
            suites = SuiteRepository(session, organization_id)
            started: list[tuple[TestExecution, str]] = []
            for suite_id in selection.suite_ids[:MAX_SUITES]:
                suite = await suites.get_in_project(project.id, uuid.UUID(suite_id))
                name = suite.name if suite else suite_id
                try:
                    async with session.begin_nested():
                        view, _ = await service.start_automated(
                            organization_id=organization_id,
                            project=project,
                            data=ExecutionCreate(
                                environment_id=row.environment_id, suite_id=uuid.UUID(suite_id)
                            ),
                            automation=Automation(
                                trigger="github_webhook",
                                actor_type="system",
                                git_context={
                                    "source": "github",
                                    "link_id": str(row.id),
                                    "repository_id": row.github_repo_id,
                                    "repository": row.full_name,
                                    "head_sha": head_sha,
                                    "head_ref": subject.get("head_ref") or subject.get("branch"),
                                    "base_ref": subject.get("base_ref"),
                                    **({"pull_request": int(number)} if number is not None else {}),
                                    "suite": name,
                                    "selection": selection.as_dict(),
                                },
                                # One execution per suite and commit, however many events arrive.
                                idempotency_key=f"gh:{row.id}:{head_sha}:{suite_id}",
                            ),
                        )
                    started.append((view.execution, name))
                except AppError as exc:
                    notes.append(f"{name}: {exc.detail or exc.title}")
            delivery.execution_ids = [str(execution.id) for execution, _ in started]
            delivery.outcome = "processed" if started else "failed"
            delivery.reason = " ".join(notes)[:1000] or None
            delivery.subject = {**subject, "selection": selection.as_dict()}
            publish = [(e.id, name, e.status, e.verdict, dict(e.totals)) for e, name in started]
            full_name, enabled = row.full_name, row.status_enabled

        if enabled:
            for execution_id, name, status, verdict, totals in publish:
                await self._status(
                    connection, full_name, head_sha, name, status, verdict, totals,
                    f"{self._settings.public_base_url.rstrip('/')}/projects/{project.id}/executions/{execution_id}",
                )  # fmt: skip
        return f"started:{len(publish)}"

    async def _status(
        self,
        connection: Integration,
        full_name: str,
        sha: str,
        suite: str | None,
        status: str,
        verdict: str | None,
        totals: dict[str, Any],
        url: str,
    ) -> str | None:
        """Write one commit status. Returns an error code instead of raising."""
        state = commit_status(ci_outcome(status, verdict, totals))
        try:
            async with self._clients.for_connection(connection) as client:
                await client.set_status(
                    full_name, sha, state=state.state, context=status_context(suite),
                    description=state.description, target_url=url,
                )  # fmt: skip
        except (GitHubError, AppError) as exc:
            code = exc.code if isinstance(exc, GitHubError) else "not_connected"
            logger.warning("github_status_failed", extra={"repository": full_name, "code": code})
            return code
        return None

    async def publish(self, *, organization_id: uuid.UUID, execution_id: uuid.UUID) -> str:
        """After an execution ends: its commit status, and the pull request comment.

        Only for executions a webhook started, and only what the link has switched
        on. A failure here is recorded with the execution and changes nothing else.
        """
        async with self._session_factory() as session, session.begin():
            await set_current_org(session, organization_id)
            executions = ExecutionRepository(session, organization_id)
            execution = await executions.get(execution_id)
            context = dict(execution.git_context or {}) if execution else {}
            if execution is None or context.get("source") != "github" or not context.get("link_id"):
                return "not_from_github"
            row = await session.get(GithubRepository, uuid.UUID(str(context["link_id"])))
            connection = await session.get(Integration, row.integration_id) if row else None
            project = await session.get(Project, execution.project_id)
            if row is None or connection is None or project is None or not row.enabled:
                return "link_gone"
            if row.github_repo_id != context.get("repository_id"):
                return "link_changed"
            sha = str(context["head_sha"])
            raw_number = context.get("pull_request")
            number: int | None = int(raw_number) if raw_number is not None else None
            published: dict[str, Any] = {}
            status_args = (
                row.full_name, sha, context.get("suite"), execution.status, execution.verdict,
                dict(execution.totals), self._report_url(execution),
            )  # fmt: skip
            want_status, want_comment = (
                row.status_enabled,
                row.comment_enabled and number is not None,
            )
            body, comment_id = None, None
            if want_comment and number is not None:
                runs = []
                for other in await executions.for_commit(project.id, row.github_repo_id, sha):
                    report = await build_execution_report(session, organization_id, project, other)
                    runs.append(
                        RunSummary(
                            report,
                            ci_outcome(other.status, other.verdict, other.totals),
                            self._report_url(other),
                        )
                    )
                body = pr_comment(runs, head_sha=sha, selection=context.get("selection"))
                comment_id = await executions.last_comment_id(
                    project.id, row.github_repo_id, number
                )
            full_name = row.full_name

        if want_status:
            published["status"] = await self._status(connection, *status_args) or "ok"
        if want_comment and body is not None and number is not None:
            try:
                async with self._clients.for_connection(connection) as client:
                    if comment_id is not None:
                        try:
                            await client.update_comment(full_name, comment_id, body)
                        except GitHubError as exc:
                            if exc.code != "not_found":
                                raise
                            comment_id = None  # the comment was deleted: write a new one
                    if comment_id is None:
                        comment_id = await client.create_comment(full_name, number, body)
                published["comment"] = "ok"
            except (GitHubError, AppError) as exc:
                code = exc.code if isinstance(exc, GitHubError) else "not_connected"
                logger.warning(
                    "github_comment_failed", extra={"repository": full_name, "code": code}
                )
                published["comment"] = code
        if not published:
            return "nothing_enabled"

        async with self._session_factory() as session, session.begin():
            await set_current_org(session, organization_id)
            execution = await ExecutionRepository(session, organization_id).get(execution_id)
            if execution is not None:
                context = dict(execution.git_context or {})
                context["published"] = published
                if published.get("comment") == "ok" and comment_id is not None:
                    context["comment_id"] = comment_id
                execution.git_context = context
        return json.dumps(published, sort_keys=True)
