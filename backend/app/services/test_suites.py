"""Test suites: grouping, ordering and approving cases."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ErrorDetail, NotFoundError, ValidationFailedError
from app.core.security.permissions import Permission
from app.db.errors import is_unique_violation
from app.models import TestCase, TestSuite, TestSuiteCase
from app.repositories.instructions import InstructionRepository
from app.repositories.projects import ApplicationRepository
from app.repositories.test_cases import CaseVersionRepository, SuiteRepository, TestCaseRepository
from app.schemas.test_cases import SuiteCreate, SuiteUpdate
from app.services.audit import AuditEvent, AuditService
from app.services.projects import ProjectContext
from app.services.test_cases import approve_case


@dataclass(frozen=True, slots=True)
class SuiteMember:
    case: TestCase
    position: int
    current_version: int | None
    # The version that was approved with the suite, if the suite is approved.
    approved_version: int | None

    @property
    def outdated(self) -> bool:
        """The case has been changed since the suite was approved."""
        return self.approved_version is not None and self.approved_version != self.current_version


@dataclass(frozen=True, slots=True)
class SuiteView:
    suite: TestSuite
    members: list[SuiteMember]


class SuiteService:
    def __init__(self, *, session: AsyncSession, audit: AuditService) -> None:
        self._session = session
        self._audit = audit

    def _suites(self, ctx: ProjectContext) -> SuiteRepository:
        return SuiteRepository(self._session, ctx.organization_id)

    # --- reads -------------------------------------------------------------------

    async def list(
        self, ctx: ProjectContext, *, feature: str | None, status: str | None
    ) -> list[SuiteView]:
        suites = await self._suites(ctx).list_in_project(
            ctx.project.id, feature=feature, status=status
        )
        return [await self.view(ctx, suite) for suite in suites]

    async def get(self, ctx: ProjectContext, suite_id: uuid.UUID) -> TestSuite:
        suite = await self._suites(ctx).get_in_project(ctx.project.id, suite_id)
        if suite is None:
            raise NotFoundError("Test suite not found.")
        return suite

    async def view(self, ctx: ProjectContext, suite: TestSuite) -> SuiteView:
        links = (await self._suites(ctx).members([suite.id]))[suite.id]
        cases = {
            c.id: c
            for c in await TestCaseRepository(self._session, ctx.organization_id).list_in_project(
                ctx.project.id, ids=[link.test_case_id for link in links]
            )
        }
        version_ids = [
            v
            for link in links
            for v in (
                link.approved_version_id,
                cases[link.test_case_id].current_version_id if link.test_case_id in cases else None,
            )
            if v is not None
        ]
        versions = await CaseVersionRepository(self._session, ctx.organization_id).by_ids(
            version_ids
        )

        def number(version_id: uuid.UUID | None) -> int | None:
            version = versions.get(version_id) if version_id else None
            return version.version if version else None

        return SuiteView(
            suite,
            [
                SuiteMember(
                    case=cases[link.test_case_id],
                    position=link.position,
                    current_version=number(cases[link.test_case_id].current_version_id),
                    approved_version=number(link.approved_version_id),
                )
                for link in links
                if link.test_case_id in cases
            ],
        )

    # --- writing -----------------------------------------------------------------

    async def create(self, ctx: ProjectContext, data: SuiteCreate) -> TestSuite:
        ctx.require(Permission.CASES_MANAGE)
        ctx.require_writable()
        application = await ApplicationRepository(
            self._session, ctx.organization_id
        ).get_in_project(ctx.project.id, data.application_id)
        if application is None:
            raise ValidationFailedError(
                "The application is not in this project.", code="unknown_target"
            )
        suite = TestSuite(
            organization_id=ctx.organization_id,
            project_id=ctx.project.id,
            application_id=application.id,
            name=data.name,
            description=data.description,
            feature=data.feature,
            priority=data.priority,
            tags=list(data.tags),
            source="manual",
            status="draft",
            created_by=ctx.user.id,
        )
        self._session.add(suite)
        await self._flush()
        if data.case_ids:
            await self._set_cases(ctx, suite, data.case_ids)
        await self._record(ctx, "test_suite.create", suite, {"name": suite.name})
        return suite

    async def update(self, ctx: ProjectContext, suite: TestSuite, data: SuiteUpdate) -> TestSuite:
        ctx.require(Permission.CASES_MANAGE)
        ctx.require_writable()
        fields = data.model_fields_set
        for name in ("name", "priority"):
            if getattr(data, name) is not None:
                setattr(suite, name, getattr(data, name))
        for name in ("description", "feature"):
            if name in fields:
                setattr(suite, name, getattr(data, name))
        if data.tags is not None:
            suite.tags = list(data.tags)
        if data.archived is not None:
            suite.status = "archived" if data.archived else "draft"
            if not data.archived:
                await self._suites(ctx).reopen([suite.id])
        await self._flush()
        await self._record(ctx, "test_suite.update", suite, {"fields": sorted(fields)})
        return suite

    async def delete(self, ctx: ProjectContext, suite: TestSuite) -> None:
        """Removes the suite. Its cases stay in the project."""
        ctx.require(Permission.CASES_MANAGE)
        ctx.require_writable()
        await self._suites(ctx).replace_members(suite.id, [])
        suite.deleted_at = datetime.now(UTC)
        await self._session.flush()
        await self._record(ctx, "test_suite.delete", suite, {"name": suite.name})

    async def set_cases(
        self, ctx: ProjectContext, suite: TestSuite, case_ids: Sequence[uuid.UUID]
    ) -> TestSuite:
        """Replace the suite's cases. The order given is the execution order."""
        ctx.require(Permission.CASES_MANAGE)
        ctx.require_writable()
        await self._set_cases(ctx, suite, case_ids)
        await self._record(ctx, "test_suite.set_cases", suite, {"cases": len(case_ids)})
        return suite

    async def approve(self, ctx: ProjectContext, suite: TestSuite) -> TestSuite:
        """Approve the suite and every case in it, pinning the approved version of each.

        All or nothing: if one case is not ready, nothing is approved.
        """
        ctx.require(Permission.CASES_APPROVE)
        ctx.require_writable()
        if suite.status == "archived":
            raise ConflictError("An archived suite cannot be approved.", code="suite_archived")
        suites = self._suites(ctx)
        links = (await suites.members([suite.id]))[suite.id]
        if not links:
            raise ConflictError("The suite has no test cases.", code="suite_empty")
        cases = TestCaseRepository(self._session, ctx.organization_id)

        problems: list[ErrorDetail] = []
        approved: list[tuple[TestSuiteCase, TestCase]] = []
        for link in links:
            case = await cases.get(link.test_case_id)
            if case is None:
                continue
            try:
                # Re-checked now, even if the case was approved earlier.
                await approve_case(self._session, ctx, case)
                approved.append((link, case))
            except ConflictError as exc:
                problems.extend(exc.errors)
        if problems:
            # Raising rolls the whole request back, including cases approved in this loop.
            raise ConflictError(
                "Some test cases are not ready, so the suite was not approved.",
                code="suite_not_ready",
                errors=problems,
            )

        for link, case in approved:
            link.approved_version_id = case.approved_version_id
        suite.status = "approved"
        suite.approved_by = ctx.user.id
        suite.approved_at = datetime.now(UTC)
        if suite.instruction_id is not None:
            instruction = await InstructionRepository(self._session, ctx.organization_id).get(
                suite.instruction_id
            )
            if instruction is not None and instruction.status == "awaiting_approval":
                instruction.status = "completed"
        await self._session.flush()
        await self._record(ctx, "test_suite.approve", suite, {"cases": len(approved)})
        return suite

    # --- helpers -----------------------------------------------------------------

    async def _set_cases(
        self, ctx: ProjectContext, suite: TestSuite, case_ids: Sequence[uuid.UUID]
    ) -> None:
        if len(set(case_ids)) != len(case_ids):
            raise ValidationFailedError(
                "A case can be in a suite only once.", code="duplicate_case"
            )
        found = await TestCaseRepository(self._session, ctx.organization_id).list_in_project(
            ctx.project.id, ids=list(case_ids)
        )
        if len(found) != len(case_ids):
            raise ValidationFailedError(
                "Some test cases do not exist in this project.",
                code="unknown_case",
                errors=[ErrorDetail(field="case_ids", code="unknown_case", message="not found")],
            )
        suites = self._suites(ctx)
        await suites.replace_members(suite.id, case_ids)
        # Different content or order than what was approved: it is a draft again.
        await suites.reopen([suite.id])
        await self._session.refresh(suite)

    async def _flush(self) -> None:
        try:
            await self._session.flush()
        except IntegrityError as exc:
            if is_unique_violation(exc):
                raise ConflictError(
                    "A suite with this name already exists in the project.", code="name_taken"
                ) from exc
            raise

    async def _record(
        self, ctx: ProjectContext, action: str, suite: TestSuite, changes: dict[str, Any]
    ) -> None:
        await self._audit.record(
            self._session,
            AuditEvent(
                action=action,
                actor_id=ctx.user.id,
                organization_id=ctx.organization_id,
                resource_type="test_suite",
                resource_id=suite.id,
                changes=changes,
            ),
        )
