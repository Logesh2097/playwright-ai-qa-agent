"""Test credentials: nothing secret may come back out, be logged, or sit in the database in clear."""

import json
import logging
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.security.crypto import CredentialCipher, DecryptionError, EncryptedSecret
from app.db.context import set_current_org
from app.models import Credential
from app.services.audit import AuditService
from app.services.credentials import CredentialService
from tests.conftest import Account, Api, Workspace

pytestmark = pytest.mark.integration

USERNAME = "cust01-login@acme.io"
PASSWORD_VALUE = "Zx9!vK-unguessable-77"
SECRET = {"username": USERNAME, "password": PASSWORD_VALUE}


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "customer",
        "role_label": "customer",
        "kind": "username_password",
        "secret": dict(SECRET),
        "is_test_account": True,
        "setup_instructions": "Log in through the header link. The account has 500 loyalty points.",
        "login_recipe": [
            {"action": "goto", "path": "/login"},
            {
                "action": "fill",
                "target": {"role": "textbox", "name": "Email"},
                "value": "{{cred.username}}",
            },
            {
                "action": "fill",
                "target": {"role": "textbox", "name": "Password"},
                "value": "{{cred.password}}",
            },
            {"action": "click", "target": {"role": "button", "name": "Sign in"}},
            {"action": "expect", "text": "Welcome back"},
        ],
    }
    body.update(overrides)
    return body


async def _workspace(api: Api) -> tuple[Account, Workspace]:
    owner = await api.register("owner@acme.io")
    return owner, await owner.setup_environment()


def _no_secret(*texts: str) -> None:
    for value in texts:
        assert PASSWORD_VALUE not in value
        assert USERNAME not in value


# --- nothing secret comes back ----------------------------------------------------------


async def test_secret_values_never_appear_in_any_response(api: Api) -> None:
    owner, ws = await _workspace(api)
    created = await owner.post(ws.credentials_url, json=_body())
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["field_names"] == ["password", "username"]
    assert (body["source"], body["is_test_account"], body["external_ref"]) == ("stored", True, None)
    assert body["login_recipe"][1]["value"] == "{{cred.username}}"
    assert set(body) == {
        "id", "environment_id", "name", "role_label", "kind", "source", "field_names",
        "external_ref", "is_test_account", "setup_instructions", "login_recipe",
        "last_rotated_at", "created_at", "updated_at",
    }  # fmt: skip

    url = f"{ws.credentials_url}/{body['id']}"
    responses = [
        created,
        await owner.get(url),
        await owner.get(ws.credentials_url),
        await owner.patch(url, json={"role_label": "shopper"}),
        await owner.get(ws.environment_url),
        await owner.get(ws.application_url),
        await owner.get(f"/api/v1/organizations/{owner.org_id}/audit-logs"),
    ]
    assert all(r.status_code == 200 or r.status_code == 201 for r in responses)
    _no_secret(*(r.text for r in responses))
    assert (await owner.get(ws.environment_url)).json()["credential_count"] == 1


async def test_secret_is_encrypted_at_rest(api: Api, owner_engine: AsyncEngine) -> None:
    owner, ws = await _workspace(api)
    await owner.post(ws.credentials_url, json=_body())

    async with owner_engine.connect() as connection:
        row = (await connection.execute(text("SELECT * FROM credentials"))).mappings().one()
        dump = (
            await connection.execute(text("SELECT credentials::text FROM credentials"))
        ).scalar()
        audit = (
            await connection.execute(text("SELECT string_agg(changes::text, ' ') FROM audit_logs"))
        ).scalar()

    assert row["source"] == "stored"
    assert row["key_version"] == 1
    assert len(row["nonce"]) == 12
    for value in (PASSWORD_VALUE, USERNAME):
        assert value.encode() not in bytes(row["ciphertext"])
        assert value not in str(dump)
        assert value not in str(audit)
    # No column of the table holds plaintext: the set of secret-bearing columns is fixed.
    assert {"ciphertext", "nonce", "wrapped_dek"} <= set(row)
    assert not {"password", "secret", "username", "token", "value"} & set(row)


async def test_secrets_are_not_logged(api: Api, caplog: pytest.LogCaptureFixture) -> None:
    owner, ws = await _workspace(api)
    with caplog.at_level(logging.DEBUG):
        created = await owner.post(ws.credentials_url, json=_body())
        url = f"{ws.credentials_url}/{created.json()['id']}"
        await owner.patch(
            url, json={"secret": {"username": USERNAME, "password": PASSWORD_VALUE + "-v2"}}
        )
        # A rejected request must not echo what was submitted either.
        await owner.post(
            ws.credentials_url, json=_body(name="second", secret={"password": PASSWORD_VALUE})
        )
        await owner.delete(url)
    logged = "\n".join(
        record.getMessage() + json.dumps(record.__dict__, default=str) for record in caplog.records
    )
    _no_secret(logged)


async def test_validation_errors_do_not_echo_the_secret(api: Api) -> None:
    owner, ws = await _workspace(api)
    cases = [
        _body(secret={"password": PASSWORD_VALUE}),  # missing username
        _body(secret={**SECRET, "extra": PASSWORD_VALUE}),
        _body(kind="bearer_token"),  # wrong fields for the kind
        _body(is_test_account=False),
        _body(secret=None),
        _body(external_ref="env:STAGING_PW"),  # both secret and reference
        _body(secret={"Bad Key": PASSWORD_VALUE}),
    ]
    for body in cases:
        response = await owner.post(
            ws.credentials_url, json={k: v for k, v in body.items() if v is not None}
        )
        assert response.status_code == 422, response.text
        _no_secret(response.text)
    assert (await owner.get(ws.credentials_url)).json() == []


async def test_only_test_accounts_may_be_stored(api: Api, owner_engine: AsyncEngine) -> None:
    owner, ws = await _workspace(api)
    response = await owner.post(ws.credentials_url, json=_body(is_test_account=False))
    assert (response.status_code, response.json()["code"]) == (422, "test_account_required")

    # The database refuses it as well, whatever the application does.
    created = await owner.post(ws.credentials_url, json=_body())
    async with owner_engine.connect() as connection:
        with pytest.raises(Exception, match="test_account_only"):
            await connection.execute(
                text("UPDATE credentials SET is_test_account = false WHERE id = :id"),
                {"id": created.json()["id"]},
            )


def _recipe_mentioning(text: str) -> list[dict[str, Any]]:
    """A valid recipe that carries `text` in the one place free text is allowed."""
    recipe = list(_body()["login_recipe"])
    recipe[-1] = {"action": "expect", "text": text}
    return recipe


async def test_login_recipe_must_be_executable_and_use_placeholders(api: Api) -> None:
    owner, ws = await _workspace(api)
    steps = _body()["login_recipe"]
    literal = [*steps[:2], {**steps[2], "value": PASSWORD_VALUE}, *steps[3:]]
    for recipe in (
        literal,  # a value typed in, instead of {{cred.password}}
        steps[:-1],  # nothing that checks the login worked
        steps[1:],  # does not say which page to open
        [{"action": "run_script", "code": "1"}],  # not a known step
        [{**steps[0], "path": "https://elsewhere.example/login"}, *steps[1:]],  # leaves the site
    ):
        response = await owner.post(ws.credentials_url, json=_body(login_recipe=recipe))
        assert response.status_code == 422, recipe
        # Validation errors must not repeat what was sent.
        _no_secret(response.text.replace(USERNAME, ""))


async def test_secret_typed_into_recipe_or_instructions_is_refused(api: Api) -> None:
    """Recipes and instructions are stored and shown in clear, so they must use placeholders."""
    owner, ws = await _workspace(api)
    in_recipe = _body(login_recipe=_recipe_mentioning(PASSWORD_VALUE))
    in_notes = _body(setup_instructions=f"The password is {PASSWORD_VALUE}")
    for body in (in_recipe, in_notes):
        response = await owner.post(ws.credentials_url, json=body)
        assert (response.status_code, response.json()["code"]) == (422, "secret_in_plain_text")
        _no_secret(response.text.replace(USERNAME, ""))

    # It is also caught when the recipe is edited later, against the stored secret.
    created = await owner.post(ws.credentials_url, json=_body())
    url = f"{ws.credentials_url}/{created.json()['id']}"
    later = await owner.patch(url, json={"login_recipe": _recipe_mentioning(PASSWORD_VALUE)})
    assert (later.status_code, later.json()["code"]) == (422, "secret_in_plain_text")


# --- kinds, references, rotation -----------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "secret"),
    [
        ("username_password", {"username": "u", "password": "p-123456"}),
        ("basic_auth", {"username": "u", "password": "p-123456"}),
        ("bearer_token", {"token": "tok-abcdef-123456"}),
        ("api_key_header", {"header_name": "X-Api-Key", "value": "key-abcdef-123456"}),
        ("cookie", {"name": "session", "value": "cookie-abcdef-123456"}),
        ("totp_secret", {"secret": "JBSWY3DPEHPK3PXP"}),
        ("secret_value", {"value": "plain-secret-123456"}),
    ],
)
async def test_each_credential_kind_stores_exactly_its_fields(
    api: Api, kind: str, secret: dict[str, str]
) -> None:
    owner, ws = await _workspace(api)
    response = await owner.post(
        ws.credentials_url,
        json=_body(kind=kind, secret=secret, login_recipe=[], setup_instructions=None),
    )
    assert response.status_code == 201, response.text
    assert response.json()["field_names"] == sorted(secret)
    for value in secret.values():
        if len(value) > 8:
            assert value not in response.text


async def test_secret_reference_stores_only_the_pointer(
    api: Api, owner_engine: AsyncEngine
) -> None:
    owner, ws = await _workspace(api)
    response = await owner.post(
        ws.credentials_url,
        json=_body(secret=None, external_ref="env:STAGING_CUSTOMER_PASSWORD") | {"secret": None},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["source"], body["external_ref"], body["field_names"]) == (
        "reference",
        "env:STAGING_CUSTOMER_PASSWORD",
        [],
    )
    async with owner_engine.connect() as connection:
        row = (
            await connection.execute(text("SELECT ciphertext, wrapped_dek FROM credentials"))
        ).one()
    assert row.ciphertext is None and row.wrapped_dek is None

    for bad in (
        "STAGING_PASSWORD",
        "vault://secret/x",
        "env:lower_case",
        "env:",
        "file:/etc/passwd",
    ):
        refused = await owner.post(
            ws.credentials_url, json=_body(name="bad", external_ref=bad) | {"secret": None}
        )
        assert refused.status_code == 422, bad


async def test_rotation_replaces_the_secret_and_is_audited(
    api: Api, app_engine: AsyncEngine
) -> None:
    owner, ws = await _workspace(api)
    created = await owner.post(ws.credentials_url, json=_body())
    url = f"{ws.credentials_url}/{created.json()['id']}"
    assert created.json()["last_rotated_at"] is None

    rotated = await owner.patch(
        url, json={"secret": {"username": USERNAME, "password": "brand-new-pw-999"}}
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["last_rotated_at"] is not None
    assert "brand-new-pw-999" not in rotated.text

    revealed = await _reveal(api, app_engine, owner, created.json()["id"])
    assert revealed == {"username": USERNAME, "password": "brand-new-pw-999"}

    audit = (await owner.get(f"/api/v1/organizations/{owner.org_id}/audit-logs")).json()["items"]
    actions = [item["action"] for item in audit if item["resource_type"] == "credential"]
    assert actions == ["credential.use", "credential.rotate", "credential.create"]

    # Switching a stored secret to a reference wipes the ciphertext.
    switched = await owner.patch(url, json={"external_ref": "env:CUSTOMER_PW"})
    assert (switched.json()["source"], switched.json()["field_names"]) == ("reference", [])


async def _reveal(
    api: Api, app_engine: AsyncEngine, owner: Account, credential_id: str
) -> dict[str, str]:
    """Decrypt the way the test engine will: through the service, never through HTTP."""
    from app.repositories.identity import (
        MembershipRepository,
        OrganizationRepository,
        UserRepository,
    )
    from app.services.organizations import OrgContext
    from app.services.projects import ProjectService

    org_id = uuid.UUID(owner.org_id)
    async with AsyncSession(app_engine, expire_on_commit=False) as session, session.begin():
        await set_current_org(session, org_id)
        credential = await session.get(Credential, uuid.UUID(credential_id))
        assert credential is not None
        user = await UserRepository(session).get(uuid.UUID(owner.user_id))
        organization = await OrganizationRepository(session).get_active(org_id)
        membership = await MembershipRepository(session, org_id).get_active_for_user(
            uuid.UUID(owner.user_id)
        )
        assert user and organization and membership
        audit = AuditService(api.app.state.session_factory)
        project_id = (await session.execute(text("SELECT id FROM projects LIMIT 1"))).scalar_one()
        ctx = await ProjectService(session=session, audit=audit).resolve(
            OrgContext(user=user, organization=organization, membership=membership), project_id
        )
        service = CredentialService(
            session=session, audit=audit, cipher=api.app.state.credential_cipher
        )
        return await service.reveal_for_execution(ctx, credential)


async def test_stored_secret_decrypts_only_for_its_own_credential_and_tenant(
    api: Api, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    owner, ws = await _workspace(api)
    created = await owner.post(ws.credentials_url, json=_body())
    assert await _reveal(api, app_engine, owner, created.json()["id"]) == SECRET

    async with owner_engine.connect() as connection:
        row = (await connection.execute(text("SELECT * FROM credentials"))).mappings().one()
    cipher: CredentialCipher = api.app.state.credential_cipher
    encrypted = EncryptedSecret(
        bytes(row["ciphertext"]), bytes(row["nonce"]), bytes(row["wrapped_dek"]), row["key_version"]
    )

    assert (
        cipher.decrypt(encrypted, organization_id=row["organization_id"], credential_id=row["id"])
        == SECRET
    )
    # The same bytes under another tenant's or another credential's identity are useless.
    with pytest.raises(DecryptionError):
        cipher.decrypt(encrypted, organization_id=uuid.uuid4(), credential_id=row["id"])
    with pytest.raises(DecryptionError):
        cipher.decrypt(
            encrypted, organization_id=row["organization_id"], credential_id=uuid.uuid4()
        )


async def test_there_is_no_route_that_reveals_a_secret(api: Api) -> None:
    """Structural check on the API surface itself."""
    schema = (await api.client().get("/api/openapi.json")).json()
    response_schema = schema["components"]["schemas"]["CredentialResponse"]["properties"]
    assert not {"secret", "password", "token", "value", "ciphertext", "wrapped_dek"} & set(
        response_schema
    )
    credential_paths = [path for path in schema["paths"] if "/credentials" in path]
    assert len(credential_paths) == 2
    assert not any(
        word in path for path in schema["paths"] for word in ("reveal", "decrypt", "plaintext")
    )
    # `secret` is accepted on the way in only.
    assert "secret" in schema["components"]["schemas"]["CredentialCreate"]["properties"]


# --- permissions and references -----------------------------------------------------------


async def test_credential_permissions(api: Api) -> None:
    owner = await api.register("owner@acme.io")
    developer = await api.invite_and_join(owner, "dev@acme.io", "developer")
    viewer = await api.invite_and_join(owner, "viewer@acme.io", "viewer")
    project = await owner.create_project()
    for account in (developer, viewer):
        await owner.post(
            f"{owner.projects_url}/{project['id']}/members", json={"user_id": account.user_id}
        )
    application = await owner.create_application(project["id"])
    staging = await owner.create_environment(project["id"], application["id"])
    production = await owner.create_environment(
        project["id"],
        application["id"],
        name="production",
        kind="production",
        base_url="https://prod.acme.io",
    )
    base = f"{owner.projects_url}/{project['id']}/applications/{application['id']}/environments"
    staging_creds = f"{base}/{staging['id']}/credentials"
    production_creds = f"{base}/{production['id']}/credentials"

    # Viewers cannot even list credential metadata.
    assert (await viewer.get(staging_creds)).status_code == 403
    assert (await viewer.post(staging_creds, json=_body())).status_code == 403

    # Developers manage non-production credentials...
    created = await developer.post(staging_creds, json=_body())
    assert created.status_code == 201
    assert (await developer.get(staging_creds)).status_code == 200
    # ...but production credentials are for admins.
    denied = await developer.post(production_creds, json=_body())
    assert (denied.status_code, denied.json()["code"]) == (403, "admin_required")
    assert (await owner.post(production_creds, json=_body())).status_code == 201

    # Someone outside the project cannot reach them at all.
    outsider = await api.invite_and_join(owner, "qa@acme.io", "qa_engineer")
    assert (await outsider.get(staging_creds)).status_code == 404


async def test_names_are_unique_per_environment_and_references_are_protected(api: Api) -> None:
    owner, ws = await _workspace(api)
    created = await owner.post(ws.credentials_url, json=_body())
    url = f"{ws.credentials_url}/{created.json()['id']}"

    duplicate = await owner.post(ws.credentials_url, json=_body())
    assert (duplicate.status_code, duplicate.json()["code"]) == (409, "name_taken")

    await owner.patch(
        ws.environment_url,
        json={"auth_config": {"method": "form_login", "default_credential": "customer"}},
    )
    for response in (await owner.delete(url), await owner.patch(url, json={"name": "renamed"})):
        assert (response.status_code, response.json()["code"]) == (409, "credential_in_use")

    await owner.patch(ws.environment_url, json={"auth_config": {"method": "none"}})
    assert (await owner.delete(url)).status_code == 204
    assert (await owner.get(url)).status_code == 404
