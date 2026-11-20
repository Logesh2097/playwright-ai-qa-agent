"""Projects: lifecycle, permissions, membership and tenant isolation."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import Account, Api

pytestmark = pytest.mark.integration

SECRET = {"username": "cust01@acme.io", "password": "Tr1cky-Pa55word!"}


async def _team(api: Api) -> dict[str, Account]:
    owner = await api.register("owner@acme.io")
    team = {"owner": owner}
    for role in ("admin", "qa_engineer", "developer", "viewer"):
        team[role] = await api.invite_and_join(owner, f"{role}@acme.io", role)
    return team


# --- lifecycle -----------------------------------------------------------------------


async def test_create_view_update_and_list_a_project(api: Api) -> None:
    owner = await api.register("owner@acme.io")
    created = await owner.post(
        owner.projects_url, json={"name": "Web Shop", "description": "Customer storefront"}
    )
    assert created.status_code == 201, created.text
    project = created.json()
    assert project["slug"] == "web-shop"
    assert project["status"] == "active"
    assert project["organization_id"] == owner.org_id
    assert project["created_by"] == owner.user_id
    assert (project["member_count"], project["application_count"]) == (1, 0)
    assert (project["my_role"], project["can_manage"]) == ("manager", True)
    assert project["created_at"] and project["updated_at"]

    url = f"{owner.projects_url}/{project['id']}"
    updated = await owner.patch(url, json={"name": "Shop", "description": None})
    assert updated.status_code == 200
    assert (updated.json()["name"], updated.json()["description"]) == ("Shop", None)
    assert updated.json()["slug"] == "web-shop", "the slug is stable"

    assert (await owner.get(url)).json()["name"] == "Shop"
    assert [p["name"] for p in (await owner.get(owner.projects_url)).json()] == ["Shop"]


async def test_project_names_may_repeat_but_slugs_stay_unique(api: Api) -> None:
    owner = await api.register("owner@acme.io")
    first = await owner.create_project("Shop")
    second = await owner.create_project("Shop")
    assert first["slug"] == "shop"
    assert second["slug"].startswith("shop-") and second["slug"] != "shop"


@pytest.mark.parametrize(
    "body", [{}, {"name": ""}, {"name": "x" * 121}, {"name": "ok", "owner": "me"}]
)
async def test_project_input_is_validated(api: Api, body: dict[str, str]) -> None:
    owner = await api.register("owner@acme.io")
    assert (await owner.post(owner.projects_url, json=body)).status_code == 422


async def test_archived_project_is_read_only_until_restored(api: Api) -> None:
    owner = await api.register("owner@acme.io")
    ws = await owner.setup_environment()

    archived = await owner.post(f"{ws.project_url}/archive")
    assert archived.json()["status"] == "archived"

    # Still readable...
    assert (await owner.get(ws.project_url)).status_code == 200
    assert (await owner.get(ws.environment_url)).status_code == 200
    # ...but nothing can be changed.
    for response in (
        await owner.patch(ws.project_url, json={"name": "New"}),
        await owner.post(
            f"{ws.project_url}/applications", json={"name": "A", "base_url": "https://acme.io"}
        ),
        await owner.patch(ws.application_url, json={"name": "Renamed"}),
        await owner.patch(ws.environment_url, json={"name": "renamed"}),
        await owner.post(
            ws.credentials_url,
            json={
                "name": "c",
                "role_label": "customer",
                "kind": "username_password",
                "secret": SECRET,
                "is_test_account": True,
            },
        ),
    ):
        assert (response.status_code, response.json()["code"]) == (409, "project_archived")

    restored = await owner.post(f"{ws.project_url}/restore")
    assert restored.json()["status"] == "active"
    assert (await owner.patch(ws.project_url, json={"name": "New"})).status_code == 200


async def test_delete_safeguards(api: Api, owner_engine: AsyncEngine) -> None:
    owner = await api.register("owner@acme.io")
    ws = await owner.setup_environment()
    await owner.post(
        ws.credentials_url,
        json={
            "name": "customer",
            "role_label": "customer",
            "kind": "username_password",
            "secret": SECRET,
            "is_test_account": True,
        },
    )

    # 1. Must be archived first.
    not_archived = await owner.delete(ws.project_url, params={"confirm_name": "Shop"})
    assert (not_archived.status_code, not_archived.json()["code"]) == (409, "project_not_archived")
    await owner.post(f"{ws.project_url}/archive")

    # 2. The name must be typed exactly.
    assert (await owner.delete(ws.project_url)).status_code == 422
    wrong = await owner.delete(ws.project_url, params={"confirm_name": "shop"})
    assert (wrong.status_code, wrong.json()["code"]) == (422, "confirmation_mismatch")

    # 3. Not while tests are running.
    async with owner_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO test_executions (id, organization_id, project_id, environment_id, status)"
                " VALUES (:id, :org, :project, :env, 'running')"
            ),
            {
                "id": uuid.uuid4(),
                "org": owner.org_id,
                "project": ws.project_id,
                "env": ws.environment_id,
            },
        )
    busy = await owner.delete(ws.project_url, params={"confirm_name": "Shop"})
    assert (busy.status_code, busy.json()["code"]) == (409, "project_has_active_executions")
    async with owner_engine.begin() as connection:
        await connection.execute(text("UPDATE test_executions SET status = 'completed'"))

    # Now it goes through.
    assert (await owner.delete(ws.project_url, params={"confirm_name": "Shop"})).status_code == 204
    assert (await owner.get(ws.project_url)).status_code == 404
    assert (await owner.get(ws.environment_url)).status_code == 404
    assert (await owner.get(owner.projects_url)).json() == []

    async with owner_engine.connect() as connection:
        # Rows are kept for history, but the secrets are gone for good.
        assert (await connection.execute(text("SELECT count(*) FROM projects"))).scalar() == 1
        assert (await connection.execute(text("SELECT count(*) FROM credentials"))).scalar() == 0
        deleted = await connection.execute(
            text("SELECT count(*) FROM environments WHERE deleted_at IS NOT NULL")
        )
        assert deleted.scalar() == 1


# --- permissions -----------------------------------------------------------------------


async def test_who_can_create_projects(api: Api) -> None:
    team = await _team(api)
    statuses = {
        role: (
            await account.post(account.projects_url, json={"name": f"{role} project"})
        ).status_code
        for role, account in team.items()
    }
    assert statuses == {
        "owner": 201,
        "admin": 201,
        "qa_engineer": 201,
        "developer": 201,
        "viewer": 403,
    }


async def test_project_visibility_follows_membership(api: Api) -> None:
    team = await _team(api)
    project = await team["developer"].create_project("Dev Project")
    url = f"{team['owner'].projects_url}/{project['id']}"

    # The creator and organization admins see it; other members do not, until added.
    seen = {role: (await account.get(url)).status_code for role, account in team.items()}
    assert seen == {"owner": 200, "admin": 200, "developer": 200, "qa_engineer": 404, "viewer": 404}
    listed = {role: len((await a.get(a.projects_url)).json()) for role, a in team.items()}
    assert listed == {"owner": 1, "admin": 1, "developer": 1, "qa_engineer": 0, "viewer": 0}

    added = await team["developer"].post(
        f"{url}/members", json={"user_id": team["viewer"].user_id, "role": "member"}
    )
    assert added.status_code == 201
    assert (await team["viewer"].get(url)).status_code == 200
    assert (await team["viewer"].get(url)).json()["can_manage"] is False
    assert (await team["qa_engineer"].get(url)).status_code == 404

    # Admins who are not members have no project role but may manage.
    admin_view = (await team["admin"].get(url)).json()
    assert (admin_view["my_role"], admin_view["can_manage"]) == (None, True)


async def test_project_edit_archive_and_delete_permissions(api: Api) -> None:
    team = await _team(api)
    project = await team["developer"].create_project("Dev Project")
    url = f"{team['owner'].projects_url}/{project['id']}"
    for role in ("qa_engineer", "viewer"):
        await team["owner"].post(f"{url}/members", json={"user_id": team[role].user_id})

    # Plain members cannot edit, archive or manage members.
    for role in ("qa_engineer", "viewer"):
        member = team[role]
        assert (await member.patch(url, json={"name": "x"})).status_code == 403
        assert (await member.post(f"{url}/archive")).status_code == 403
        add = await member.post(f"{url}/members", json={"user_id": team["admin"].user_id})
        assert add.status_code == 403

    # The manager can edit and archive, but only admins and owners can delete.
    manager = team["developer"]
    assert (await manager.patch(url, json={"name": "Renamed"})).status_code == 200
    assert (await manager.post(f"{url}/archive")).status_code == 200
    for role in ("developer", "qa_engineer", "viewer"):
        denied = await team[role].delete(url, params={"confirm_name": "Renamed"})
        assert denied.status_code == 403
    assert (await team["admin"].delete(url, params={"confirm_name": "Renamed"})).status_code == 204


async def test_viewer_cannot_be_an_effective_manager(api: Api) -> None:
    """A project role never grants more than the organization role allows."""
    team = await _team(api)
    project = await team["owner"].create_project()
    url = f"{team['owner'].projects_url}/{project['id']}"
    await team["owner"].post(
        f"{url}/members", json={"user_id": team["viewer"].user_id, "role": "manager"}
    )
    assert (await team["viewer"].get(url)).json()["can_manage"] is False
    assert (await team["viewer"].patch(url, json={"name": "x"})).status_code == 403
    register = await team["viewer"].post(
        f"{url}/applications", json={"name": "A", "base_url": "https://acme.io"}
    )
    assert register.status_code == 403


# --- members ---------------------------------------------------------------------------


async def test_member_management(api: Api) -> None:
    team = await _team(api)
    owner, developer, qa = team["owner"], team["developer"], team["qa_engineer"]
    project = await owner.create_project()
    members_url = f"{owner.projects_url}/{project['id']}/members"

    added = await owner.post(members_url, json={"user_id": developer.user_id, "role": "member"})
    assert added.status_code == 201
    assert {m["email"]: m["role"] for m in added.json()} == {
        "owner@acme.io": "manager",
        "developer@acme.io": "member",
    }
    entry = next(m for m in added.json() if m["email"] == "developer@acme.io")
    assert entry["organization_role"] == "developer"
    assert entry["full_name"] and entry["added_at"]

    duplicate = await owner.post(members_url, json={"user_id": developer.user_id})
    assert (duplicate.status_code, duplicate.json()["code"]) == (409, "already_member")

    promoted = await owner.patch(f"{members_url}/{developer.user_id}", json={"role": "manager"})
    assert promoted.status_code == 200
    # As manager, the developer can now add others.
    assert (await developer.post(members_url, json={"user_id": qa.user_id})).status_code == 201

    # Anyone can leave; removing someone else needs a manager.
    assert (await qa.delete(f"{members_url}/{developer.user_id}")).status_code == 403
    assert (await qa.delete(f"{members_url}/{qa.user_id}")).status_code == 204
    assert (await qa.get(members_url)).status_code == 404
    assert (await owner.delete(f"{members_url}/{uuid.uuid4()}")).status_code == 404


async def test_only_organization_members_can_be_added(api: Api) -> None:
    owner = await api.register("owner@acme.io")
    outsider = await api.register("owner@evil.io", organization="Evil")
    project = await owner.create_project()
    members_url = f"{owner.projects_url}/{project['id']}/members"

    foreign = await owner.post(members_url, json={"user_id": outsider.user_id})
    unknown = await owner.post(members_url, json={"user_id": str(uuid.uuid4())})
    assert foreign.status_code == unknown.status_code == 422
    assert foreign.json()["code"] == unknown.json()["code"] == "not_an_organization_member"
    assert len((await owner.get(members_url)).json()) == 1


async def test_leaving_the_organization_ends_project_access(
    api: Api, owner_engine: AsyncEngine
) -> None:
    owner = await api.register("owner@acme.io")
    developer = await api.invite_and_join(owner, "dev@acme.io", "developer")
    project = await developer.create_project("Dev Project")
    org_members = (await owner.get(f"/api/v1/organizations/{owner.org_id}/members")).json()
    member_id = next(m["id"] for m in org_members if m["email"] == "dev@acme.io")

    await owner.delete(f"/api/v1/organizations/{owner.org_id}/members/{member_id}")

    assert (await developer.get(f"{owner.projects_url}/{project['id']}")).status_code == 404
    async with owner_engine.connect() as connection:
        remaining = await connection.execute(text("SELECT count(*) FROM project_members"))
        assert remaining.scalar() == 0
    # The project itself survives and stays reachable for admins.
    assert (await owner.get(f"{owner.projects_url}/{project['id']}")).status_code == 200


# --- tenant isolation --------------------------------------------------------------------


async def test_another_tenant_cannot_reach_projects_or_anything_below_them(api: Api) -> None:
    victim = await api.register("owner@acme.io", organization="Acme")
    ws = await victim.setup_environment()
    created = await victim.post(
        ws.credentials_url,
        json={
            "name": "customer",
            "role_label": "customer",
            "kind": "username_password",
            "secret": SECRET,
            "is_test_account": True,
        },
    )
    credential_url = f"{ws.credentials_url}/{created.json()['id']}"
    attacker = await api.register("owner@evil.io", organization="Evil")
    own = await attacker.setup_environment()

    victim_urls = [
        ws.project_url,
        f"{ws.project_url}/members",
        f"{ws.project_url}/applications",
        ws.application_url,
        ws.environments_url,
        ws.environment_url,
        ws.credentials_url,
        credential_url,
    ]
    for url in victim_urls:
        # Through the victim's organization: not a member -> 404.
        assert (await attacker.get(url)).status_code == 404, url
        # Through the attacker's own organization with the victim's IDs: not found either.
        swapped = url.replace(victim.org_id, attacker.org_id)
        assert (await attacker.get(swapped)).status_code == 404, swapped

    # Mixing own parents with the victim's children does not work either.
    mixed = [
        f"{own.project_url}/applications/{ws.application_id}",
        f"{own.application_url}/environments/{ws.environment_id}",
        f"{own.environment_url}/credentials/{created.json()['id']}",
    ]
    for url in mixed:
        assert (await attacker.get(url)).status_code == 404, url
        assert (await attacker.patch(url, json={"name": "pwned"})).status_code == 404, url
        assert (await attacker.delete(url)).status_code == 404, url

    writes = [
        await attacker.patch(ws.project_url, json={"name": "pwned"}),
        await attacker.post(f"{ws.project_url}/archive"),
        await attacker.delete(ws.project_url, params={"confirm_name": "Shop"}),
        await attacker.post(f"{ws.project_url}/members", json={"user_id": attacker.user_id}),
        await attacker.post(f"{ws.environment_url}/access-check"),
    ]
    assert [r.status_code for r in writes] == [404] * 5

    assert (await victim.get(ws.project_url)).json()["name"] == "Shop"
    assert len((await victim.get(ws.credentials_url)).json()) == 1
    assert [p["id"] for p in (await attacker.get(attacker.projects_url)).json()] == [own.project_id]
