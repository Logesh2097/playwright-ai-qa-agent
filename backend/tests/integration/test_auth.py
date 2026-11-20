"""Registration, login, sessions and password flows against the real stack."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.core.security.tokens import create_access_token
from tests.conftest import PASSWORD, Account, Api

pytestmark = pytest.mark.integration

AUTH = "/api/v1/auth"
NEW_PASSWORD = "an entirely different passphrase"


async def _scalar(engine: AsyncEngine, sql: str, **params: object) -> object:
    async with engine.connect() as connection:
        return (await connection.execute(text(sql), params)).scalar()


# --- registration -----------------------------------------------------------------


async def test_register_creates_user_organization_and_owner_membership(
    api: Api, owner_engine: AsyncEngine
) -> None:
    account = await api.register("Asha@Acme.io", organization="Acme Corp")

    assert account.session["user"]["email"] == "Asha@acme.io"  # domain is normalised
    assert account.session["token_type"] == "bearer"
    assert account.session["expires_in"] == 900
    membership = account.session["memberships"][0]
    assert membership["organization_name"] == "Acme Corp"
    assert membership["organization_slug"] == "acme-corp"
    assert membership["role"] == "owner"

    stored = await _scalar(owner_engine, "SELECT password_hash FROM users")
    assert isinstance(stored, str) and stored.startswith("$argon2id$")


async def test_register_response_never_contains_password_or_refresh_token(api: Api) -> None:
    client = api.client()
    response = await client.post(
        f"{AUTH}/register",
        json={
            "email": "a@acme.io",
            "password": PASSWORD,
            "full_name": "A",
            "organization_name": "Acme",
        },
    )
    assert PASSWORD not in response.text
    assert "password" not in response.text
    refresh = response.cookies["qa_refresh_token"]
    assert refresh not in response.text
    assert response.headers["cache-control"] == "no-store"


async def test_refresh_cookie_is_httponly_secure_strict_and_path_scoped(api: Api) -> None:
    response = await api.client().post(
        f"{AUTH}/register",
        json={
            "email": "a@acme.io",
            "password": PASSWORD,
            "full_name": "A",
            "organization_name": "Acme",
        },
    )
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=strict" in cookie
    assert "path=/api/v1/auth" in cookie


async def test_register_rejects_duplicate_email_case_insensitively(api: Api) -> None:
    await api.register("asha@acme.io")
    response = await api.client().post(
        f"{AUTH}/register",
        json={
            "email": "ASHA@acme.io",
            "password": PASSWORD,
            "full_name": "Other",
            "organization_name": "Other",
        },
    )
    assert response.status_code == 409
    assert response.json()["code"] == "email_taken"


@pytest.mark.parametrize(
    ("overrides", "status", "code"),
    [
        ({"password": "short"}, 422, "weak_password"),
        ({"password": "password1234"}, 422, "weak_password"),
        ({"email": "not-an-email"}, 422, "validation_error"),
        ({"full_name": "   "}, 422, "validation_error"),
        ({"organization_name": None}, 422, "validation_error"),
        ({"invite_token": "x" * 43}, 422, "validation_error"),  # both org name and invite
        ({"is_platform_admin": True}, 422, "validation_error"),  # unknown fields are refused
    ],
)
async def test_register_validation(
    api: Api, owner_engine: AsyncEngine, overrides: dict[str, object], status: int, code: str
) -> None:
    body: dict[str, object] = {
        "email": "asha@acme.io",
        "password": PASSWORD,
        "full_name": "Asha Rao",
        "organization_name": "Acme",
    }
    body.update(overrides)
    response = await api.client().post(
        f"{AUTH}/register", json={k: v for k, v in body.items() if v is not None}
    )
    assert response.status_code == status, response.text
    assert response.json()["code"] == code
    assert str(body["password"]) not in response.text
    assert await _scalar(owner_engine, "SELECT count(*) FROM users") == 0


# --- login ------------------------------------------------------------------------


async def test_login_returns_tokens_and_me_works(api: Api) -> None:
    await api.register("asha@acme.io")
    account = await api.login("asha@acme.io")

    me = await account.get(f"{AUTH}/me")
    assert me.status_code == 200
    assert me.json()["user"]["email"] == "asha@acme.io"
    assert me.json()["user"]["last_login_at"] is not None
    assert [m["role"] for m in me.json()["memberships"]] == ["owner"]


async def test_invalid_credentials_are_indistinguishable(
    api: Api, owner_engine: AsyncEngine
) -> None:
    await api.register("asha@acme.io")
    await api.register("inactive@acme.io", organization="Other")
    async with owner_engine.begin() as connection:
        await connection.execute(
            text("UPDATE users SET is_active = false WHERE email = 'inactive@acme.io'")
        )

    client = api.client()
    attempts = [
        {"email": "asha@acme.io", "password": "the wrong password!"},
        {"email": "nobody@acme.io", "password": PASSWORD},
        {"email": "inactive@acme.io", "password": PASSWORD},
    ]
    bodies = []
    for attempt in attempts:
        response = await client.post(f"{AUTH}/login", json=attempt)
        assert response.status_code == 401
        assert "set-cookie" not in response.headers
        body = response.json()
        body.pop("request_id")
        bodies.append(body)

    assert bodies[0] == bodies[1] == bodies[2]
    assert bodies[0]["code"] == "invalid_credentials"
    assert bodies[0]["detail"] == "Invalid email or password."


async def test_account_is_locked_after_repeated_failures(api: Api) -> None:
    await api.register("asha@acme.io")
    client = api.client()
    wrong = {"email": "asha@acme.io", "password": "the wrong password!"}

    for _ in range(3):  # login_max_failures in the test settings
        assert (await client.post(f"{AUTH}/login", json=wrong)).status_code == 401

    locked = await client.post(
        f"{AUTH}/login", json={"email": "asha@acme.io", "password": PASSWORD}
    )
    assert locked.status_code == 429
    assert locked.json()["code"] == "account_locked"
    assert int(locked.headers["retry-after"]) > 0

    # Another account is unaffected.
    await api.register("other@acme.io", organization="Other")
    await api.login("other@acme.io")


async def test_successful_login_clears_the_failure_count(api: Api) -> None:
    await api.register("asha@acme.io")
    client = api.client()
    wrong = {"email": "asha@acme.io", "password": "the wrong password!"}
    for _ in range(2):
        await client.post(f"{AUTH}/login", json=wrong)
    await api.login("asha@acme.io")
    for _ in range(2):
        assert (await client.post(f"{AUTH}/login", json=wrong)).status_code == 401
    await api.login("asha@acme.io")


async def test_failed_logins_are_audited_without_secrets(
    api: Api, owner_engine: AsyncEngine
) -> None:
    await api.register("asha@acme.io")
    await api.client().post(
        f"{AUTH}/login", json={"email": "asha@acme.io", "password": "the wrong password!"}
    )
    async with owner_engine.connect() as connection:
        rows = (
            await connection.execute(text("SELECT action, changes::text, ip FROM audit_logs"))
        ).all()
    assert "auth.login_failed" in {row.action for row in rows}
    assert all("wrong password" not in row[1] for row in rows)
    failed = next(row for row in rows if row.action == "auth.login_failed")
    assert failed.ip is not None


# --- access tokens ------------------------------------------------------------------


async def test_me_requires_a_valid_access_token(api: Api, integration_settings: Settings) -> None:
    account = await api.register("asha@acme.io")
    client = api.client()

    missing = await client.get(f"{AUTH}/me")
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"

    garbage = await client.get(f"{AUTH}/me", headers={"Authorization": "Bearer nonsense"})
    assert (garbage.status_code, garbage.json()["code"]) == (401, "invalid_token")

    forged = create_access_token(
        user_id=uuid.UUID(account.user_id),
        session_id=uuid.uuid4(),
        secret="a-different-signing-key-0123456789abcdef",
        ttl_s=900,
        issuer=integration_settings.app_name,
    )
    response = await client.get(f"{AUTH}/me", headers={"Authorization": f"Bearer {forged}"})
    assert (response.status_code, response.json()["code"]) == (401, "invalid_token")


async def test_expired_access_token_is_rejected(api: Api, integration_settings: Settings) -> None:
    account = await api.register("asha@acme.io")
    expired = create_access_token(
        user_id=uuid.UUID(account.user_id),
        session_id=uuid.uuid4(),
        secret=integration_settings.jwt_secret_key.get_secret_value(),
        ttl_s=900,
        issuer=integration_settings.app_name,
        now=datetime.now(UTC) - timedelta(minutes=16),
    )
    response = await api.client().get(f"{AUTH}/me", headers={"Authorization": f"Bearer {expired}"})
    assert response.status_code == 401
    assert response.json()["code"] == "token_expired"


async def test_token_of_a_deactivated_user_stops_working(
    api: Api, owner_engine: AsyncEngine
) -> None:
    account = await api.register("asha@acme.io")
    async with owner_engine.begin() as connection:
        await connection.execute(text("UPDATE users SET is_active = false"))
    assert (await account.get(f"{AUTH}/me")).status_code == 401


# --- refresh ------------------------------------------------------------------------


async def test_refresh_rotates_the_token_and_issues_a_new_access_token(api: Api) -> None:
    account = await api.register("asha@acme.io")
    before = account.client.cookies["qa_refresh_token"]

    response = await account.client.post(f"{AUTH}/refresh")
    assert response.status_code == 200, response.text
    after = account.client.cookies["qa_refresh_token"]
    assert after != before
    assert set(response.json()) == {"access_token", "token_type", "expires_in"}

    me = await account.client.get(
        f"{AUTH}/me", headers={"Authorization": f"Bearer {response.json()['access_token']}"}
    )
    assert me.status_code == 200


async def test_reusing_a_rotated_refresh_token_ends_the_whole_session(api: Api) -> None:
    account = await api.register("asha@acme.io")
    stolen = account.client.cookies["qa_refresh_token"]
    rotated = await account.client.post(f"{AUTH}/refresh")
    assert rotated.status_code == 200

    replay = await api.client().post(
        f"{AUTH}/refresh", headers={"Cookie": f"qa_refresh_token={stolen}"}
    )
    assert replay.status_code == 401
    assert replay.json()["code"] == "invalid_refresh_token"

    # The legitimate client's newer refresh token and access token are dead too.
    assert (await account.client.post(f"{AUTH}/refresh")).status_code == 401
    me = await account.client.get(
        f"{AUTH}/me", headers={"Authorization": f"Bearer {rotated.json()['access_token']}"}
    )
    assert (me.status_code, me.json()["code"]) == (401, "session_revoked")


async def test_refresh_without_cookie_or_with_unknown_token_fails(api: Api) -> None:
    client = api.client()
    assert (await client.post(f"{AUTH}/refresh")).status_code == 401
    unknown = await client.post(
        f"{AUTH}/refresh", headers={"Cookie": f"qa_refresh_token={'x' * 43}"}
    )
    assert unknown.status_code == 401


async def test_expired_refresh_token_is_rejected(api: Api, owner_engine: AsyncEngine) -> None:
    account = await api.register("asha@acme.io")
    async with owner_engine.begin() as connection:
        await connection.execute(
            text("UPDATE refresh_tokens SET expires_at = now() - interval '1 minute'")
        )
    response = await account.client.post(f"{AUTH}/refresh")
    assert (response.status_code, response.json()["code"]) == (401, "invalid_refresh_token")


async def test_cookie_endpoints_require_the_csrf_header(api: Api) -> None:
    account = await api.register("asha@acme.io")
    for path in ("refresh", "logout"):
        response = await account.client.post(f"{AUTH}/{path}", headers={"X-Requested-With": ""})
        assert response.status_code == 400
        assert response.json()["code"] == "csrf_header_missing"
    # The session survived both attempts.
    assert (await account.client.post(f"{AUTH}/refresh")).status_code == 200


async def test_refresh_tokens_are_stored_hashed(api: Api, owner_engine: AsyncEngine) -> None:
    account = await api.register("asha@acme.io")
    raw = account.client.cookies["qa_refresh_token"]
    stored = await _scalar(owner_engine, "SELECT token_hash FROM refresh_tokens")
    assert stored != raw
    assert isinstance(stored, str) and len(stored) == 64


# --- logout and session invalidation ----------------------------------------------


async def test_logout_invalidates_refresh_and_access_tokens_immediately(api: Api) -> None:
    account = await api.register("asha@acme.io")
    assert (await account.get(f"{AUTH}/me")).status_code == 200

    response = await account.client.post(f"{AUTH}/logout")
    assert response.status_code == 204
    assert "qa_refresh_token" not in account.client.cookies

    me = await account.get(f"{AUTH}/me")
    assert (me.status_code, me.json()["code"]) == (401, "session_revoked")


async def test_logout_only_ends_the_current_session(api: Api) -> None:
    await api.register("asha@acme.io")
    laptop = await api.login("asha@acme.io")
    phone = await api.login("asha@acme.io")

    await laptop.client.post(f"{AUTH}/logout")

    assert (await laptop.get(f"{AUTH}/me")).status_code == 401
    assert (await phone.get(f"{AUTH}/me")).status_code == 200
    assert (await phone.client.post(f"{AUTH}/refresh")).status_code == 200


async def test_logout_all_ends_every_session(api: Api) -> None:
    await api.register("asha@acme.io")
    laptop = await api.login("asha@acme.io")
    phone = await api.login("asha@acme.io")

    assert (await laptop.post(f"{AUTH}/logout-all")).status_code == 204

    for device in (laptop, phone):
        assert (await device.get(f"{AUTH}/me")).status_code == 401
    assert (await phone.client.post(f"{AUTH}/refresh")).status_code == 401


# --- passwords ----------------------------------------------------------------------


async def test_change_password_requires_the_current_password(api: Api) -> None:
    account = await api.register("asha@acme.io")
    response = await account.post(
        f"{AUTH}/password/change",
        json={"current_password": "not my password!!", "new_password": NEW_PASSWORD},
    )
    assert (response.status_code, response.json()["code"]) == (400, "wrong_password")
    await api.login("asha@acme.io")  # unchanged


async def test_change_password_ends_other_sessions_and_keeps_the_caller_signed_in(
    api: Api,
) -> None:
    await api.register("asha@acme.io")
    laptop = await api.login("asha@acme.io")
    phone = await api.login("asha@acme.io")

    response = await laptop.post(
        f"{AUTH}/password/change",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert response.status_code == 200, response.text

    assert (await phone.get(f"{AUTH}/me")).status_code == 401
    assert (await laptop.get(f"{AUTH}/me")).status_code == 401  # the old access token
    fresh = Account(client=laptop.client, email=laptop.email, session=response.json())
    assert (await fresh.get(f"{AUTH}/me")).status_code == 200

    old = await api.client().post(
        f"{AUTH}/login", json={"email": "asha@acme.io", "password": PASSWORD}
    )
    assert old.status_code == 401
    await api.login("asha@acme.io", NEW_PASSWORD)


async def test_change_password_applies_the_policy(api: Api) -> None:
    account = await api.register("asha@acme.io")
    response = await account.post(
        f"{AUTH}/password/change",
        json={"current_password": PASSWORD, "new_password": "password1234"},
    )
    assert (response.status_code, response.json()["code"]) == (422, "weak_password")
    assert (await account.get(f"{AUTH}/me")).status_code == 200


async def test_forgot_password_answers_the_same_for_unknown_and_known_emails(api: Api) -> None:
    await api.register("asha@acme.io")
    client = api.client()

    unknown = await client.post(f"{AUTH}/password/forgot", json={"email": "nobody@acme.io"})
    known = await client.post(f"{AUTH}/password/forgot", json={"email": "asha@acme.io"})

    assert unknown.status_code == known.status_code == 202
    assert unknown.json() == known.json() == {"status": "accepted"}
    assert [message.to for message in api.outbox.messages] == ["asha@acme.io"]
    assert "https://qa.example/reset-password?token=" in api.outbox.messages[0].body


async def test_password_reset_flow(api: Api, owner_engine: AsyncEngine) -> None:
    await api.register("asha@acme.io")
    existing = await api.login("asha@acme.io")
    client = api.client()
    await client.post(f"{AUTH}/password/forgot", json={"email": "asha@acme.io"})
    token = api.outbox.token_for("asha@acme.io")

    stored = await _scalar(owner_engine, "SELECT token_hash FROM password_reset_tokens")
    assert stored != token, "only a hash of the reset token is stored"

    response = await client.post(
        f"{AUTH}/password/reset", json={"token": token, "new_password": NEW_PASSWORD}
    )
    assert response.status_code == 204

    # Sessions that existed before the reset are over.
    assert (await existing.get(f"{AUTH}/me")).status_code == 401
    assert (await existing.client.post(f"{AUTH}/refresh")).status_code == 401
    await api.login("asha@acme.io", NEW_PASSWORD)

    # The token works once.
    again = await client.post(
        f"{AUTH}/password/reset", json={"token": token, "new_password": "yet another passphrase"}
    )
    assert (again.status_code, again.json()["code"]) == (400, "invalid_reset_token")


async def test_password_reset_rejects_expired_unknown_and_superseded_tokens(
    api: Api, owner_engine: AsyncEngine
) -> None:
    await api.register("asha@acme.io")
    client = api.client()

    unknown = await client.post(
        f"{AUTH}/password/reset", json={"token": "x" * 43, "new_password": NEW_PASSWORD}
    )
    assert unknown.status_code == 400

    await client.post(f"{AUTH}/password/forgot", json={"email": "asha@acme.io"})
    first = api.outbox.token_for("asha@acme.io")
    await client.post(f"{AUTH}/password/forgot", json={"email": "asha@acme.io"})
    second = api.outbox.token_for("asha@acme.io")

    superseded = await client.post(
        f"{AUTH}/password/reset", json={"token": first, "new_password": NEW_PASSWORD}
    )
    assert superseded.status_code == 400

    async with owner_engine.begin() as connection:
        await connection.execute(
            text("UPDATE password_reset_tokens SET expires_at = now() - interval '1 second'")
        )
    expired = await client.post(
        f"{AUTH}/password/reset", json={"token": second, "new_password": NEW_PASSWORD}
    )
    assert (expired.status_code, expired.json()["code"]) == (400, "invalid_reset_token")
    await api.login("asha@acme.io")  # password unchanged


async def test_weak_password_does_not_consume_the_reset_token(api: Api) -> None:
    await api.register("asha@acme.io")
    client = api.client()
    await client.post(f"{AUTH}/password/forgot", json={"email": "asha@acme.io"})
    token = api.outbox.token_for("asha@acme.io")

    weak = await client.post(
        f"{AUTH}/password/reset", json={"token": token, "new_password": "password1234"}
    )
    assert (weak.status_code, weak.json()["code"]) == (422, "weak_password")

    ok = await client.post(
        f"{AUTH}/password/reset", json={"token": token, "new_password": NEW_PASSWORD}
    )
    assert ok.status_code == 204


async def test_password_reset_requests_are_rate_limited(api: Api) -> None:
    await api.register("asha@acme.io")
    client = api.client()
    statuses = [
        (await client.post(f"{AUTH}/password/forgot", json={"email": "asha@acme.io"})).status_code
        for _ in range(4)
    ]
    assert statuses == [202, 202, 202, 429]
    assert len(api.outbox.messages) == 3


async def test_reset_unlocks_a_locked_account(api: Api) -> None:
    await api.register("asha@acme.io")
    client = api.client()
    for _ in range(3):
        await client.post(
            f"{AUTH}/login", json={"email": "asha@acme.io", "password": "the wrong password!"}
        )
    await client.post(f"{AUTH}/password/forgot", json={"email": "asha@acme.io"})
    await client.post(
        f"{AUTH}/password/reset",
        json={"token": api.outbox.token_for("asha@acme.io"), "new_password": NEW_PASSWORD},
    )
    await api.login("asha@acme.io", NEW_PASSWORD)
