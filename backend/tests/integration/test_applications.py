"""Applications and environments: registration, URL validation, isolation, access checks."""

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import PUBLIC_IP, Account, Api

pytestmark = pytest.mark.integration


async def _app(owner: Account, **overrides: Any) -> tuple[str, dict[str, Any]]:
    project = await owner.create_project()
    application = await owner.create_application(project["id"], **overrides)
    return f"{owner.projects_url}/{project['id']}/applications", application


# --- applications --------------------------------------------------------------------


async def test_register_an_application_with_all_fields(api: Api) -> None:
    owner = await api.register("owner@acme.io")
    project = await owner.create_project()
    response = await owner.post(
        f"{owner.projects_url}/{project['id']}/applications",
        json={
            "name": "Storefront",
            "description": "Customer-facing shop",
            "base_url": "https://acme.io",
            "type": "web",
            "framework": "Vue 3 + Laravel",
            "auth_method": "form_login",
            "default_browser_config": {
                "browser": "firefox",
                "viewport": {"width": 1280, "height": 720},
                "locale": "en-IN",
            },
            "allowed_domains": ["ACME.io", "acme-cdn.net"],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["allowed_domains"] == ["acme-cdn.net", "acme.io"]
    assert body["default_browser_config"]["browser"] == "firefox"
    assert (body["framework"], body["auth_method"], body["status"]) == (
        "Vue 3 + Laravel",
        "form_login",
        "active",
    )
    assert body["environment_count"] == 0


async def test_allowed_domains_default_to_the_base_url_host(api: Api) -> None:
    owner = await api.register("owner@acme.io")
    _, application = await _app(owner, base_url="https://Shop.Acme.io/app")
    assert application["allowed_domains"] == ["shop.acme.io"]


async def test_several_applications_per_project_with_unique_names(api: Api) -> None:
    owner = await api.register("owner@acme.io")
    project = await owner.create_project()
    await owner.create_application(project["id"], name="Storefront")
    await owner.create_application(project["id"], name="Admin API", type="api")
    url = f"{owner.projects_url}/{project['id']}/applications"

    duplicate = await owner.post(url, json={"name": "Storefront", "base_url": "https://acme.io"})
    assert (duplicate.status_code, duplicate.json()["code"]) == (409, "name_taken")
    listed = (await owner.get(url)).json()
    assert [a["name"] for a in listed] == ["Admin API", "Storefront"]
    assert (await owner.get(f"{owner.projects_url}/{project['id']}")).json()[
        "application_count"
    ] == 2


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"base_url": "ftp://acme.io"}, "invalid_scheme"),
        ({"base_url": "https://user:pw@acme.io"}, "credentials_in_url"),
        ({"base_url": "not a url"}, "invalid_url"),
        ({"base_url": "https://acme.io", "allowed_domains": ["other.io"]}, "domain_not_allowed"),
        ({"allowed_domains": ["*.acme.io"]}, "invalid_domain"),
        ({"allowed_domains": ["io"]}, "invalid_domain"),
        ({"type": "desktop"}, "validation_error"),
        ({"auth_method": "magic"}, "validation_error"),
        ({"default_browser_config": {"browser": "netscape"}}, "validation_error"),
    ],
)
async def test_application_validation(api: Api, overrides: dict[str, Any], code: str) -> None:
    owner = await api.register("owner@acme.io")
    project = await owner.create_project()
    body = {"name": "Storefront", "base_url": "https://acme.io", **overrides}
    response = await owner.post(f"{owner.projects_url}/{project['id']}/applications", json=body)
    assert (response.status_code, response.json()["code"]) == (422, code), response.text


async def test_update_archive_and_delete_an_application(
    api: Api, owner_engine: AsyncEngine
) -> None:
    owner = await api.register("owner@acme.io")
    ws = await owner.setup_environment()

    updated = await owner.patch(
        ws.application_url, json={"name": "Shop UI", "framework": "Nuxt", "status": "archived"}
    )
    assert updated.status_code == 200
    assert (updated.json()["name"], updated.json()["status"]) == ("Shop UI", "archived")
    assert updated.json()["environment_count"] == 1

    assert (await owner.delete(ws.application_url)).status_code == 204
    assert (await owner.get(ws.application_url)).status_code == 404
    assert (await owner.get(ws.environment_url)).status_code == 404
    # The name is free again.
    await owner.create_application(ws.project_id, name="Shop UI")


async def test_narrowing_allowed_domains_cannot_strand_an_environment(api: Api) -> None:
    owner = await api.register("owner@acme.io")
    ws = await owner.setup_environment()  # uses staging.acme.io

    response = await owner.patch(
        ws.application_url,
        json={"base_url": "https://prod.acme.io", "allowed_domains": ["prod.acme.io"]},
    )
    assert (response.status_code, response.json()["code"]) == (422, "domain_in_use")
    assert (await owner.get(ws.application_url)).json()["allowed_domains"] == ["acme.io"]


async def test_viewers_can_read_but_not_change_applications(api: Api) -> None:
    owner = await api.register("owner@acme.io")
    viewer = await api.invite_and_join(owner, "viewer@acme.io", "viewer")
    ws = await owner.setup_environment()
    await owner.post(f"{ws.project_url}/members", json={"user_id": viewer.user_id})

    assert (await viewer.get(ws.application_url)).status_code == 200
    assert (await viewer.get(ws.environment_url)).status_code == 200
    denied = [
        await viewer.post(
            f"{ws.project_url}/applications", json={"name": "A", "base_url": "https://acme.io"}
        ),
        await viewer.patch(ws.application_url, json={"name": "x"}),
        await viewer.delete(ws.application_url),
        await viewer.post(
            ws.environments_url,
            json={"name": "e", "kind": "testing", "base_url": "https://staging.acme.io"},
        ),
        await viewer.patch(ws.environment_url, json={"name": "x"}),
        await viewer.delete(ws.environment_url),
        await viewer.post(f"{ws.environment_url}/access-check"),
        await viewer.get(ws.credentials_url),
    ]
    assert [r.status_code for r in denied] == [403] * 8


# --- environments ----------------------------------------------------------------------


async def test_create_an_environment_with_all_settings(api: Api) -> None:
    owner = await api.register("owner@acme.io")
    url, application = await _app(owner)
    response = await owner.post(
        f"{url}/{application['id']}/environments",
        json={
            "name": "Staging",
            "kind": "staging",
            "base_url": "https://staging.acme.io/app",
            "api_base_url": "https://api.staging.acme.io/v1",
            "additional_origins": [{"url": "https://auth.acme.io", "purpose": "auth_provider"}],
            "variables": [
                {"name": "FEATURE_LOYALTY", "value": "on"},
                {"name": "TENANT", "value": "demo"},
            ],
            "auth_config": {
                "method": "form_login",
                "login_url": "https://auth.acme.io/login",
                "notes": "Use the SSO form.",
            },
            "allowed_actions": ["navigate", "read", "click", "type"],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert {(o["origin"], o["purpose"]) for o in body["allowed_origins"]} == {
        ("https://staging.acme.io", "app"),
        ("https://api.staging.acme.io", "api"),
        ("https://auth.acme.io", "auth_provider"),
    }
    assert body["allowed_actions"] == ["navigate", "read", "click", "type"]
    assert body["variables"][0] == {"name": "FEATURE_LOYALTY", "value": "on", "secret_ref": None}
    assert body["auth_config"]["login_url"] == "https://auth.acme.io/login"
    assert (body["is_read_only"], body["requires_approval"], body["allow_private_network"]) == (
        False,
        False,
        False,
    )
    assert (body["status"], body["credential_count"]) == ("active", 0)


@pytest.mark.parametrize("kind", ["local", "development", "testing", "staging", "other"])
async def test_supported_environment_kinds(api: Api, kind: str) -> None:
    owner = await api.register("owner@acme.io")
    ws = await owner.setup_environment(name=kind, kind=kind)
    assert (await owner.get(ws.environment_url)).json()["kind"] == kind


@pytest.mark.parametrize(
    ("overrides", "code", "field"),
    [
        # outside the application's allowed domains
        ({"base_url": "https://evil.example"}, "domain_not_allowed", "base_url"),
        ({"base_url": "https://acme.io.evil.example"}, "domain_not_allowed", "base_url"),
        ({"api_base_url": "https://evil.example/api"}, "domain_not_allowed", "api_base_url"),
        (
            {"additional_origins": [{"url": "https://evil.example"}]},
            "domain_not_allowed",
            "additional_origins.0.url",
        ),
        # inside the domain list, but resolving somewhere it must not
        ({"base_url": "https://internal.acme.io"}, "address_not_allowed", "base_url"),
        ({"base_url": "https://meta.acme.io"}, "address_not_allowed", "base_url"),
        ({"base_url": "https://mixed.acme.io"}, "address_not_allowed", "base_url"),
        ({"base_url": "https://nowhere.acme.io"}, "host_not_resolvable", "base_url"),
        # malformed
        ({"base_url": "ftp://staging.acme.io"}, "invalid_scheme", "base_url"),
        ({"base_url": "https://u:p@staging.acme.io"}, "credentials_in_url", "base_url"),
        (
            {"auth_config": {"login_url": "https://auth.acme.io/login"}},
            "origin_not_allowlisted",
            "auth_config.login_url",
        ),
    ],
)
async def test_environment_urls_are_validated(
    api: Api, overrides: dict[str, Any], code: str, field: str
) -> None:
    owner = await api.register("owner@acme.io")
    url, application = await _app(owner)
    body = {
        "name": "staging",
        "kind": "staging",
        "base_url": "https://staging.acme.io",
        **overrides,
    }
    response = await owner.post(f"{url}/{application['id']}/environments", json=body)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == code
    assert response.json()["errors"][0]["field"] == field
    assert (await owner.get(f"{url}/{application['id']}/environments")).json() == []


async def test_loopback_and_private_targets_need_an_admin_to_permit_them(api: Api) -> None:
    owner = await api.register("owner@acme.io")
    developer = await api.invite_and_join(owner, "dev@acme.io", "developer")
    project = await owner.create_project()
    await owner.post(
        f"{owner.projects_url}/{project['id']}/members", json={"user_id": developer.user_id}
    )
    application = await owner.create_application(
        project["id"],
        base_url="http://localhost:3000",
        allowed_domains=["localhost", "acme.io", "10.0.0.5"],
    )
    environments = (
        f"{owner.projects_url}/{project['id']}/applications/{application['id']}/environments"
    )
    local = {"name": "local", "kind": "local", "base_url": "http://localhost:3000"}

    # Without the switch: refused for everyone.
    blocked = await owner.post(environments, json=local)
    assert (blocked.status_code, blocked.json()["code"]) == (422, "address_not_allowed")

    # A developer cannot turn the switch on...
    denied = await developer.post(environments, json={**local, "allow_private_network": True})
    assert (denied.status_code, denied.json()["code"]) == (403, "admin_required")
    # ...an admin can, and it is written to the audit log.
    allowed = await owner.post(environments, json={**local, "allow_private_network": True})
    assert allowed.status_code == 201, allowed.text
    assert allowed.json()["allow_private_network"] is True
    audit = (
        await owner.get(
            f"/api/v1/organizations/{owner.org_id}/audit-logs",
            params={"action": "environment.create"},
        )
    ).json()
    assert audit["items"][0]["changes"]["allow_private_network"] is True

    # Developers may edit other fields of that environment, but not widen another one.
    env_url = f"{environments}/{allowed.json()['id']}"
    assert (await developer.patch(env_url, json={"name": "my machine"})).status_code == 200
    other = await owner.post(
        environments,
        json={"name": "staging", "kind": "staging", "base_url": "https://staging.acme.io"},
    )
    widen = await developer.patch(
        f"{environments}/{other.json()['id']}", json={"allow_private_network": True}
    )
    assert widen.status_code == 403

    # Even with the switch, the metadata service stays unreachable.
    meta = await owner.post(
        environments,
        json={
            "name": "meta",
            "kind": "other",
            "base_url": "https://meta.acme.io",
            "allow_private_network": True,
        },
    )
    assert (meta.status_code, meta.json()["code"]) == (422, "address_not_allowed")


async def test_production_environments_are_protected(api: Api) -> None:
    owner = await api.register("owner@acme.io")
    developer = await api.invite_and_join(owner, "dev@acme.io", "developer")
    project = await owner.create_project()
    await owner.post(
        f"{owner.projects_url}/{project['id']}/members", json={"user_id": developer.user_id}
    )
    application = await owner.create_application(project["id"])
    environments = (
        f"{owner.projects_url}/{project['id']}/applications/{application['id']}/environments"
    )
    production = {"name": "production", "kind": "production", "base_url": "https://prod.acme.io"}

    assert (await developer.post(environments, json=production)).status_code == 403
    insecure = await owner.post(
        environments, json={**production, "base_url": "http://prod.acme.io"}
    )
    assert (insecure.status_code, insecure.json()["code"]) == (422, "https_required")

    created = await owner.post(environments, json=production)
    assert created.status_code == 201, created.text
    body = created.json()
    assert (body["is_read_only"], body["requires_approval"]) == (True, True)
    assert body["allowed_actions"] == ["navigate", "read", "api_read"]

    env_url = f"{environments}/{body['id']}"
    # Developers cannot change or delete it, nor turn another environment into production.
    assert (await developer.patch(env_url, json={"name": "prod"})).status_code == 403
    assert (await developer.delete(env_url)).status_code == 403
    staging = await developer.post(
        environments,
        json={"name": "staging", "kind": "staging", "base_url": "https://staging.acme.io"},
    )
    promote = await developer.patch(
        f"{environments}/{staging.json()['id']}", json={"kind": "production"}
    )
    assert promote.status_code == 403

    # Read-only and mutating actions contradict each other.
    conflict = await owner.patch(env_url, json={"allowed_actions": ["navigate", "click"]})
    assert (conflict.status_code, conflict.json()["code"]) == (422, "actions_conflict_read_only")


async def test_environment_variables_must_not_hold_secrets(api: Api) -> None:
    owner = await api.register("owner@acme.io")
    ws = await owner.setup_environment()

    secret = await owner.patch(
        ws.environment_url, json={"variables": [{"name": "API_TOKEN", "value": "abc123"}]}
    )
    assert (secret.status_code, secret.json()["code"]) == (422, "secret_in_variable")
    assert "abc123" not in secret.text

    unknown = await owner.patch(
        ws.environment_url, json={"variables": [{"name": "API_TOKEN", "secret_ref": "missing"}]}
    )
    assert (unknown.status_code, unknown.json()["code"]) == (422, "unknown_credential")

    await owner.post(
        ws.credentials_url,
        json={
            "name": "api-token",
            "role_label": "service",
            "kind": "secret_value",
            "secret": {"value": "sk-live-abcdef123456"},
            "is_test_account": True,
        },
    )
    ok = await owner.patch(
        ws.environment_url,
        json={
            "variables": [
                {"name": "API_TOKEN", "secret_ref": "api-token"},
                {"name": "REGION", "value": "in"},
            ]
        },
    )
    assert ok.status_code == 200, ok.text
    assert "sk-live" not in ok.text
    assert ok.json()["variables"][0] == {
        "name": "API_TOKEN",
        "value": None,
        "secret_ref": "api-token",
    }

    for bad in (
        [{"name": "A", "value": "1"}, {"name": "A", "value": "2"}],
        [{"name": "1BAD", "value": "x"}],
        [{"name": "A"}],
    ):
        assert (await owner.patch(ws.environment_url, json={"variables": bad})).status_code == 422


async def test_updating_urls_revalidates_and_replaces_the_allowlist(api: Api) -> None:
    owner = await api.register("owner@acme.io")
    ws = await owner.setup_environment()

    bad = await owner.patch(ws.environment_url, json={"base_url": "https://internal.acme.io"})
    assert (bad.status_code, bad.json()["code"]) == (422, "address_not_allowed")
    unchanged = (await owner.get(ws.environment_url)).json()
    assert [o["origin"] for o in unchanged["allowed_origins"]] == ["https://staging.acme.io"]

    moved = await owner.patch(
        ws.environment_url,
        json={
            "base_url": "https://prod.acme.io",
            "additional_origins": [{"url": "https://auth.acme.io", "purpose": "auth_provider"}],
        },
    )
    assert moved.status_code == 200, moved.text
    assert {o["origin"] for o in moved.json()["allowed_origins"]} == {
        "https://prod.acme.io",
        "https://auth.acme.io",
    }

    disabled = await owner.patch(ws.environment_url, json={"status": "disabled"})
    assert disabled.json()["status"] == "disabled"


# --- environment isolation ---------------------------------------------------------------


async def test_environments_are_isolated_from_each_other(
    api: Api, owner_engine: AsyncEngine
) -> None:
    owner = await api.register("owner@acme.io")
    project = await owner.create_project()
    application = await owner.create_application(project["id"])
    other_app = await owner.create_application(
        project["id"], name="Other", base_url="https://acme.io"
    )
    staging = await owner.create_environment(project["id"], application["id"])
    testing = await owner.create_environment(
        project["id"],
        application["id"],
        name="testing",
        kind="testing",
        base_url="https://api.staging.acme.io",
    )
    base = f"{owner.projects_url}/{project['id']}/applications"
    staging_url = f"{base}/{application['id']}/environments/{staging['id']}"
    testing_url = f"{base}/{application['id']}/environments/{testing['id']}"

    secret = {"username": "stg-user", "password": "staging-only-pw-1"}
    created = await owner.post(
        f"{staging_url}/credentials",
        json={
            "name": "customer",
            "role_label": "customer",
            "kind": "username_password",
            "secret": secret,
            "is_test_account": True,
        },
    )
    credential_id = created.json()["id"]

    # Each environment has its own allowlist and its own credentials.
    assert [o["origin"] for o in (await owner.get(staging_url)).json()["allowed_origins"]] == [
        "https://staging.acme.io"
    ]
    assert [o["origin"] for o in (await owner.get(testing_url)).json()["allowed_origins"]] == [
        "https://api.staging.acme.io"
    ]
    assert (await owner.get(f"{testing_url}/credentials")).json() == []
    assert (await owner.get(f"{testing_url}/credentials/{credential_id}")).status_code == 404
    assert (await owner.delete(f"{testing_url}/credentials/{credential_id}")).status_code == 404

    # A credential of one environment cannot be referenced from another.
    cross = await owner.patch(
        testing_url, json={"variables": [{"name": "LOGIN", "secret_ref": "customer"}]}
    )
    assert (cross.status_code, cross.json()["code"]) == (422, "unknown_credential")

    # The same credential name can exist independently in both.
    again = await owner.post(
        f"{testing_url}/credentials",
        json={
            "name": "customer",
            "role_label": "customer",
            "kind": "username_password",
            "secret": {"username": "tst", "password": "testing-only-pw-2"},
            "is_test_account": True,
        },
    )
    assert again.status_code == 201

    # An environment is only reachable through its own application.
    assert (
        await owner.get(f"{base}/{other_app['id']}/environments/{staging['id']}")
    ).status_code == 404

    # Deleting one environment removes only its own credentials.
    assert (await owner.delete(staging_url)).status_code == 204
    async with owner_engine.connect() as connection:
        names = (
            (await connection.execute(text("SELECT environment_id::text FROM credentials")))
            .scalars()
            .all()
        )
    assert names == [testing["id"]]


# --- access check ------------------------------------------------------------------------


async def test_access_check_reports_reachability_without_returning_page_content(api: Api) -> None:
    owner = await api.register("owner@acme.io")
    ws = await owner.setup_environment(base_url="https://staging.acme.io/app")
    api.network.routes["https://staging.acme.io/app"] = (302, {"location": "/app/login"})

    response = await owner.post(f"{ws.environment_url}/access-check")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reachable"] is True
    assert body["final_url"] == "https://staging.acme.io/app/login"
    assert [hop["status"] for hop in body["hops"]] == [302, 200]
    assert "secret page body" not in response.text

    # The request went to the address that was validated, carrying the real host name.
    assert all(request.url.host == PUBLIC_IP for request in api.network.requests)
    assert api.network.requests[0].headers["host"] == "staging.acme.io"


@pytest.mark.parametrize(
    ("location", "code"),
    [
        ("https://evil.example/steal", "origin_not_allowlisted"),
        ("http://169.254.169.254/latest/meta-data/", "origin_not_allowlisted"),
        (
            "https://auth.acme.io/",
            "origin_not_allowlisted",
        ),  # allowed domain, but not a listed origin
    ],
)
async def test_access_check_stops_redirects_that_leave_the_allowlist(
    api: Api, location: str, code: str
) -> None:
    owner = await api.register("owner@acme.io")
    ws = await owner.setup_environment()
    api.network.routes["https://staging.acme.io/"] = (302, {"location": location})

    body = (await owner.post(f"{ws.environment_url}/access-check")).json()
    assert body["reachable"] is False
    assert body["error_code"] == code
    assert body["hops"][-1]["blocked_code"] == code
    assert len(api.network.requests) == 1, "the redirect target was never contacted"


async def test_access_check_revalidates_dns_at_the_time_of_the_check(api: Api) -> None:
    """The host was public when the environment was saved, and is internal now."""
    owner = await api.register("owner@acme.io")
    ws = await owner.setup_environment()
    api.network.hosts["staging.acme.io"] = ["10.0.0.9"]

    body = (await owner.post(f"{ws.environment_url}/access-check")).json()
    assert (body["reachable"], body["error_code"]) == (False, "address_not_allowed")
    assert api.network.requests == []


async def test_access_checks_are_rate_limited_and_audited(api: Api) -> None:
    owner = await api.register("owner@acme.io")
    ws = await owner.setup_environment()
    api.app.state.settings.access_probe_limit = 2

    statuses = [
        (await owner.post(f"{ws.environment_url}/access-check")).status_code for _ in range(3)
    ]
    assert statuses == [200, 200, 429]
    audit = await owner.get(
        f"/api/v1/organizations/{owner.org_id}/audit-logs",
        params={"action": "environment.access_check"},
    )
    assert len(audit.json()["items"]) == 2
