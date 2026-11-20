"""Test credentials for the applications under test.

Secret values enter through `create` and `update` and leave only through
`reveal_for_execution`, which exists for the test engine and is not reachable
from any API route. Nothing here returns, logs or audits a secret value.
"""

import json
import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    AppError,
    ConflictError,
    ErrorDetail,
    ForbiddenError,
    NotFoundError,
    ValidationFailedError,
)
from app.core.security.crypto import CredentialCipher, DecryptionError, EncryptedSecret
from app.core.security.permissions import Permission, has_permission
from app.db.errors import is_unique_violation
from app.models import Credential, Environment
from app.repositories.projects import CredentialRepository
from app.schemas.projects import CredentialCreate, CredentialUpdate
from app.services.audit import AuditEvent, AuditService
from app.services.projects import ProjectContext
from app.utils.ids import uuid7

logger = logging.getLogger(__name__)

# The exact fields each kind of credential consists of.
SECRET_FIELDS: dict[str, frozenset[str]] = {
    "username_password": frozenset({"username", "password"}),
    "basic_auth": frozenset({"username", "password"}),
    "bearer_token": frozenset({"token"}),
    "api_key_header": frozenset({"header_name", "value"}),
    "cookie": frozenset({"name", "value"}),
    "totp_secret": frozenset({"secret"}),
    "secret_value": frozenset({"value"}),
}
# Fields that identify where a secret goes rather than being secret themselves.
_NON_SECRET_FIELDS = frozenset({"header_name", "name"})
_MIN_SCANNED_LENGTH = 6


class CredentialService:
    def __init__(
        self, *, session: AsyncSession, audit: AuditService, cipher: CredentialCipher
    ) -> None:
        self._session = session
        self._audit = audit
        self._cipher = cipher

    def _repository(self, ctx: ProjectContext) -> CredentialRepository:
        return CredentialRepository(self._session, ctx.organization_id)

    # --- reads (metadata only) -------------------------------------------------------

    async def list(self, ctx: ProjectContext, environment: Environment) -> Sequence[Credential]:
        ctx.require(Permission.CREDENTIALS_READ)
        return await self._repository(ctx).list_in_environment(environment.id)

    async def get(
        self, ctx: ProjectContext, environment: Environment, credential_id: uuid.UUID
    ) -> Credential:
        ctx.require(Permission.CREDENTIALS_READ)
        credential = await self._repository(ctx).get_in_environment(environment.id, credential_id)
        if credential is None:
            raise NotFoundError("Credential not found.")
        return credential

    # --- writes ----------------------------------------------------------------------

    async def create(
        self, ctx: ProjectContext, environment: Environment, data: CredentialCreate
    ) -> Credential:
        self._require_manage(ctx, environment)
        if not data.is_test_account:
            raise ValidationFailedError(
                "Only dedicated test accounts may be stored. Do not use a real person's login.",
                code="test_account_required",
                errors=[
                    ErrorDetail(
                        field="is_test_account",
                        code="test_account_required",
                        message="must be true",
                    )
                ],
            )
        credential = Credential(
            id=uuid7(),
            organization_id=ctx.organization_id,
            environment_id=environment.id,
            name=data.name,
            role_label=data.role_label,
            kind=data.kind,
            is_test_account=True,
            setup_instructions=data.setup_instructions,
            login_recipe=data.login_recipe,
            created_by=ctx.user.id,
        )
        self._set_secret(credential, secret=data.secret, external_ref=data.external_ref)
        self._require_no_plaintext_secret(data.secret, data.login_recipe, data.setup_instructions)
        self._session.add(credential)
        await self._flush()
        await self._record(ctx, "credential.create", credential, {"source": credential.source})
        return credential

    async def update(
        self,
        ctx: ProjectContext,
        environment: Environment,
        credential: Credential,
        data: CredentialUpdate,
    ) -> Credential:
        self._require_manage(ctx, environment)
        fields = data.model_fields_set
        changed: list[str] = []

        if data.name is not None and data.name != credential.name:
            self._require_unreferenced(environment, credential, action="rename")
            credential.name = data.name
            changed.append("name")
        if data.role_label is not None and data.role_label != credential.role_label:
            credential.role_label = data.role_label
            changed.append("role_label")
        if "setup_instructions" in fields:
            credential.setup_instructions = data.setup_instructions
            changed.append("setup_instructions")
        if data.login_recipe is not None:
            credential.login_recipe = data.login_recipe
            changed.append("login_recipe")

        rotated = data.secret is not None or data.external_ref is not None
        if rotated:
            self._set_secret(credential, secret=data.secret, external_ref=data.external_ref)
            credential.last_rotated_at = datetime.now(UTC)

        # Checked against the secret now in effect, whether or not it was just replaced.
        current = data.secret if data.secret is not None else self._decrypt(credential)
        self._require_no_plaintext_secret(
            current, credential.login_recipe, credential.setup_instructions
        )

        if changed or rotated:
            await self._flush()
            await self._record(
                ctx,
                "credential.rotate" if rotated else "credential.update",
                credential,
                {"fields": changed, "source": credential.source},
            )
        return credential

    async def delete(
        self, ctx: ProjectContext, environment: Environment, credential: Credential
    ) -> None:
        self._require_manage(ctx, environment)
        self._require_unreferenced(environment, credential, action="delete")
        await self._record(ctx, "credential.delete", credential, {})
        await self._repository(ctx).delete(credential)

    # --- internal use by the test engine -----------------------------------------------

    async def reveal_for_execution(
        self, ctx: ProjectContext, credential: Credential, *, execution_id: uuid.UUID | None = None
    ) -> dict[str, str]:
        """Decrypt a stored credential for a test run. Every use is audited.

        Not exposed by any route. Reference credentials are resolved by the
        runner from its own environment and never pass through here.
        """
        if credential.source != "stored":
            raise ValueError("reference credentials are resolved where the test runs")
        payload = self._decrypt(credential)
        assert payload is not None  # noqa: S101 - stored credentials always have a payload
        await self._record(
            ctx,
            "credential.use",
            credential,
            {"execution_id": str(execution_id) if execution_id else None},
            actor_type="system",
        )
        return payload

    async def reveal_for_job(
        self,
        credential: Credential,
        *,
        requested_by: uuid.UUID | None,
        purpose: str,
        reference_id: uuid.UUID,
    ) -> dict[str, str]:
        """Decrypt a stored credential inside a background job. Every use is audited.

        The job has no signed-in user; `requested_by` is whoever started it and
        was authorised when they did.
        """
        if credential.source != "stored":
            raise ValueError("reference credentials are resolved where the test runs")
        payload = self._decrypt(credential)
        assert payload is not None  # noqa: S101 - stored credentials always have a payload
        await self._audit.record(
            self._session,
            AuditEvent(
                action="credential.use",
                actor_id=requested_by,
                actor_type="system",
                organization_id=credential.organization_id,
                resource_type="credential",
                resource_id=credential.id,
                changes={
                    "name": credential.name,
                    "kind": credential.kind,
                    "environment_id": str(credential.environment_id),
                    "purpose": purpose,
                    "reference_id": str(reference_id),
                },
            ),
        )
        return payload

    # --- helpers ---------------------------------------------------------------------

    @staticmethod
    def _require_manage(ctx: ProjectContext, environment: Environment) -> None:
        ctx.require(Permission.CREDENTIALS_MANAGE)
        ctx.require_writable()
        if environment.kind == "production" and not has_permission(
            ctx.org.role, Permission.ENVIRONMENTS_SECURITY
        ):
            raise ForbiddenError(
                "Only organization admins can manage production credentials.",
                code="admin_required",
            )

    def _set_secret(
        self,
        credential: Credential,
        *,
        secret: dict[str, str] | None,
        external_ref: str | None,
    ) -> None:
        if external_ref is not None:
            credential.source = "reference"
            credential.external_ref = external_ref
            credential.ciphertext = credential.nonce = credential.wrapped_dek = None
            credential.key_version = None
            credential.field_names = []
            return
        assert secret is not None  # noqa: S101 - guaranteed by the schemas
        expected = SECRET_FIELDS[credential.kind]
        if set(secret) != expected:
            raise ValidationFailedError(
                f"A {credential.kind} credential needs exactly these fields: "
                f"{', '.join(sorted(expected))}.",
                code="invalid_secret_fields",
                errors=[
                    ErrorDetail(
                        field="secret", code="invalid_secret_fields", message="wrong fields"
                    )
                ],
            )
        encrypted = self._cipher.encrypt(
            secret, organization_id=credential.organization_id, credential_id=credential.id
        )
        credential.source = "stored"
        credential.external_ref = None
        credential.ciphertext = encrypted.ciphertext
        credential.nonce = encrypted.nonce
        credential.wrapped_dek = encrypted.wrapped_dek
        credential.key_version = encrypted.key_version
        credential.field_names = sorted(secret)

    def _decrypt(self, credential: Credential) -> dict[str, str] | None:
        if credential.source != "stored":
            return None
        assert credential.ciphertext and credential.nonce and credential.wrapped_dek  # noqa: S101
        assert credential.key_version is not None  # noqa: S101
        try:
            return self._cipher.decrypt(
                EncryptedSecret(
                    ciphertext=credential.ciphertext,
                    nonce=credential.nonce,
                    wrapped_dek=credential.wrapped_dek,
                    key_version=credential.key_version,
                ),
                organization_id=credential.organization_id,
                credential_id=credential.id,
            )
        except DecryptionError as exc:
            logger.error(
                "credential_decryption_failed", extra={"credential_id": str(credential.id)}
            )
            raise AppError(code="credential_unreadable") from exc

    @staticmethod
    def _require_no_plaintext_secret(
        secret: dict[str, str] | None, recipe: Sequence[dict[str, Any]], instructions: str | None
    ) -> None:
        """A login recipe must use {{cred.*}} placeholders, never the value itself.

        Recipes and instructions are stored unencrypted and shown in the UI, so a
        secret typed into them would defeat the encryption.
        """
        if not secret:
            return
        haystack = json.dumps(recipe) + "\n" + (instructions or "")
        for name, value in secret.items():
            if name in _NON_SECRET_FIELDS or len(value) < _MIN_SCANNED_LENGTH:
                continue
            if value in haystack or json.dumps(value)[1:-1] in haystack:
                raise ValidationFailedError(
                    f"The login recipe or instructions contain the value of '{name}'. "
                    f"Use the placeholder {{{{cred.{name}}}}} instead.",
                    code="secret_in_plain_text",
                )

    @staticmethod
    def _require_unreferenced(
        environment: Environment, credential: Credential, *, action: str
    ) -> None:
        referenced = (
            any(variable.get("secret_ref") == credential.name for variable in environment.variables)
            or environment.auth_config.get("default_credential") == credential.name
        )
        if referenced:
            raise ConflictError(
                f"The environment's variables or authentication settings refer to this credential. "
                f"Remove those references before you {action} it.",
                code="credential_in_use",
            )

    async def _flush(self) -> None:
        try:
            await self._session.flush()
        except IntegrityError as exc:
            if is_unique_violation(exc):
                raise ConflictError(
                    "A credential with this name already exists in the environment.",
                    code="name_taken",
                ) from exc
            raise

    async def _record(
        self,
        ctx: ProjectContext,
        action: str,
        credential: Credential,
        changes: dict[str, Any],
        *,
        actor_type: str = "user",
    ) -> None:
        await self._audit.record(
            self._session,
            AuditEvent(
                action=action,
                actor_id=ctx.user.id,
                actor_type=actor_type,
                organization_id=ctx.organization_id,
                resource_type="credential",
                resource_id=credential.id,
                # Names and kinds only. `redact` would also mask any key that sounds secret.
                changes={
                    "name": credential.name,
                    "kind": credential.kind,
                    "environment_id": str(credential.environment_id),
                    **changes,
                },
            ),
        )
