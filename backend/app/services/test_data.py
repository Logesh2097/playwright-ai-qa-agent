"""Test data sets and test files of a project.

Both exist so that tests use data made for testing. Whoever saves either has to
confirm that it is made up (`is_synthetic`), and values that look like real
payment cards are refused outright.
"""

import base64
import binascii
import hashlib
import re
import uuid
from collections.abc import Sequence

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.core.security.permissions import Permission
from app.db.errors import is_unique_violation
from app.models import TestDataSet, TestFile
from app.repositories.projects import ApplicationRepository, EnvironmentRepository
from app.repositories.test_data import DataSetRepository, TestFileRepository
from app.schemas.executions import DataSetWrite, TestFileCreate
from app.services.audit import AuditEvent, AuditService
from app.services.projects import ProjectContext

FILE_TYPES = frozenset(
    {"image/png", "image/jpeg", "application/pdf", "text/plain", "text/csv", "application/json"}
)
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
# Numbers published by payment providers for testing. They are not anyone's card.
_TEST_CARDS = frozenset(
    {
        "4242424242424242", "4111111111111111", "4000056655665556", "5555555555554444",
        "5200828282828210", "378282246310005", "371449635398431", "6011111111111117",
        "3056930009020004", "3566002020360505", "4000000000000002", "5105105105105100",
        "4012888888881881", "6011000990139424", "30569309025904", "38520000023237",
    }
)  # fmt: skip


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
    return total % 10 == 0


def looks_like_real_card(value: str) -> bool:
    """A valid card number that is not one of the well-known test numbers."""
    for found in _CARD.findall(value):
        digits = re.sub(r"\D", "", found)
        if 13 <= len(digits) <= 19 and _luhn(digits) and digits not in _TEST_CARDS:
            return True
    return False


class TestDataService:
    __test__ = False

    def __init__(self, *, session: AsyncSession, audit: AuditService, settings: Settings) -> None:
        self._session = session
        self._audit = audit
        self._settings = settings

    # --- data sets -------------------------------------------------------------------

    async def list_sets(self, ctx: ProjectContext) -> Sequence[TestDataSet]:
        ctx.require(Permission.PROJECTS_READ)
        return await DataSetRepository(self._session, ctx.organization_id).list_in_project(
            ctx.project.id
        )

    async def get_set(self, ctx: ProjectContext, set_id: uuid.UUID) -> TestDataSet:
        ctx.require(Permission.PROJECTS_READ)
        data_set = await DataSetRepository(self._session, ctx.organization_id).get_in_project(
            ctx.project.id, set_id
        )
        if data_set is None:
            raise NotFoundError("Test data set not found.")
        return data_set

    async def _check_values(self, ctx: ProjectContext, data: DataSetWrite) -> None:
        if not data.is_synthetic:
            raise ValidationFailedError(
                "Confirm that this is made-up test data. Real customer data must not be used for testing.",
                code="real_data_not_allowed",
            )
        for name, value in data.values.items():
            if looks_like_real_card(value):
                raise ValidationFailedError(
                    f"'{name}' looks like a real payment card number. Use a provider's test number.",
                    code="real_data_not_allowed",
                )
        if data.environment_id is not None:
            environment = await EnvironmentRepository(self._session, ctx.organization_id).get(
                data.environment_id
            )
            application = (
                await ApplicationRepository(self._session, ctx.organization_id).get(
                    environment.application_id
                )
                if environment
                else None
            )
            if application is None or application.project_id != ctx.project.id:
                raise NotFoundError("Environment not found.")

    async def create_set(self, ctx: ProjectContext, data: DataSetWrite) -> TestDataSet:
        ctx.require(Permission.CASES_MANAGE)
        ctx.require_writable()
        await self._check_values(ctx, data)
        data_set = TestDataSet(
            organization_id=ctx.organization_id,
            project_id=ctx.project.id,
            environment_id=data.environment_id,
            name=data.name,
            description=data.description,
            values=dict(data.values),
            is_synthetic=True,
            created_by=ctx.user.id,
        )
        try:
            async with self._session.begin_nested():
                await DataSetRepository(self._session, ctx.organization_id).add(data_set)
        except IntegrityError as exc:
            if is_unique_violation(exc):
                raise ConflictError(
                    "A data set with this name already exists for that environment.",
                    code="name_taken",
                ) from None
            raise
        await self._record(ctx, "test_data_set.create", data_set.id, data)
        return data_set

    async def update_set(
        self, ctx: ProjectContext, data_set: TestDataSet, data: DataSetWrite
    ) -> TestDataSet:
        ctx.require(Permission.CASES_MANAGE)
        ctx.require_writable()
        if (data.name, data.environment_id) != (data_set.name, data_set.environment_id):
            raise ConflictError(
                "The name and environment of a data set cannot be changed; create another one.",
                code="immutable_field",
            )
        await self._check_values(ctx, data)
        data_set.description = data.description
        data_set.values = dict(data.values)
        await self._session.flush()
        await self._record(ctx, "test_data_set.update", data_set.id, data)
        return data_set

    async def delete_set(self, ctx: ProjectContext, data_set: TestDataSet) -> None:
        ctx.require(Permission.CASES_MANAGE)
        ctx.require_writable()
        await DataSetRepository(self._session, ctx.organization_id).remove(data_set)
        await self._audit.record(
            self._session,
            AuditEvent(
                action="test_data_set.delete",
                actor_id=ctx.user.id,
                organization_id=ctx.organization_id,
                resource_type="test_data_set",
                resource_id=data_set.id,
                changes={"name": data_set.name},
            ),
        )

    async def _record(
        self, ctx: ProjectContext, action: str, set_id: uuid.UUID, data: DataSetWrite
    ) -> None:
        await self._audit.record(
            self._session,
            AuditEvent(
                action=action,
                actor_id=ctx.user.id,
                organization_id=ctx.organization_id,
                resource_type="test_data_set",
                resource_id=set_id,
                # Names only: the values are test data, but they need not be in the audit log.
                changes={
                    "name": data.name,
                    "environment_id": str(data.environment_id) if data.environment_id else None,
                    "value_names": sorted(data.values),
                },
            ),
        )

    # --- files -----------------------------------------------------------------------

    async def list_files(self, ctx: ProjectContext) -> Sequence[TestFile]:
        ctx.require(Permission.PROJECTS_READ)
        return await TestFileRepository(self._session, ctx.organization_id).list_in_project(
            ctx.project.id
        )

    async def create_file(self, ctx: ProjectContext, data: TestFileCreate) -> TestFile:
        """Adding a file is what approves it for upload, so it needs the approving role."""
        ctx.require(Permission.CASES_APPROVE)
        ctx.require_writable()
        if not data.is_synthetic:
            raise ValidationFailedError(
                "Confirm that this file holds no real customer data.", code="real_data_not_allowed"
            )
        if data.content_type not in FILE_TYPES:
            raise ValidationFailedError(
                "Allowed file types: " + ", ".join(sorted(FILE_TYPES)) + ".",
                code="file_type_not_allowed",
            )
        try:
            content = base64.b64decode(data.content_base64, validate=True)
        except (binascii.Error, ValueError):
            raise ValidationFailedError(
                "content_base64 is not valid base64.", code="invalid_file"
            ) from None
        if not content or len(content) > self._settings.test_file_max_bytes:
            raise ValidationFailedError(
                f"A test file must be between 1 byte and {self._settings.test_file_max_bytes} bytes.",
                code="file_size_not_allowed",
            )
        file = TestFile(
            organization_id=ctx.organization_id,
            project_id=ctx.project.id,
            name=data.name,
            content_type=data.content_type,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            content=content,
            is_synthetic=True,
            created_by=ctx.user.id,
        )
        try:
            async with self._session.begin_nested():
                await TestFileRepository(self._session, ctx.organization_id).add(file)
        except IntegrityError as exc:
            if is_unique_violation(exc):
                raise ConflictError(
                    "A test file with this name already exists.", code="name_taken"
                ) from None
            raise
        await self._record_file(ctx, "test_file.create", file)
        return file

    async def delete_file(self, ctx: ProjectContext, file_id: uuid.UUID) -> None:
        ctx.require(Permission.CASES_APPROVE)
        ctx.require_writable()
        repository = TestFileRepository(self._session, ctx.organization_id)
        file = await repository.get_in_project(ctx.project.id, file_id)
        if file is None:
            raise NotFoundError("Test file not found.")
        await self._record_file(ctx, "test_file.delete", file)
        await repository.remove(file)

    async def _record_file(self, ctx: ProjectContext, action: str, file: TestFile) -> None:
        await self._audit.record(
            self._session,
            AuditEvent(
                action=action,
                actor_id=ctx.user.id,
                organization_id=ctx.organization_id,
                resource_type="test_file",
                resource_id=file.id,
                changes={
                    "name": file.name,
                    "content_type": file.content_type,
                    "sha256": file.sha256,
                },
            ),
        )
