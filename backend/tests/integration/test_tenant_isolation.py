"""One organization must never see or change another's data.

Two layers are tested separately: the API (membership check, 404 for outsiders)
and PostgreSQL row-level security (which holds even for raw SQL with no WHERE
clause, i.e. when application code forgets to filter).
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.context import set_current_org
from app.db.errors import INSUFFICIENT_PRIVILEGE, sqlstate
from app.models import Project, tenant_table_names
from app.repositories.projects import ProjectRepository
from tests.conftest import Api

pytestmark = pytest.mark.integration

ORGS = "/api/v1/organizations"


# --- API layer -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("GET", "", None),
        ("PATCH", "", {"name": "Taken Over"}),
        ("DELETE", "", None),
        ("GET", "/members", None),
        ("POST", "/invitations", {"email": "mole@evil.io", "role": "admin"}),
        ("GET", "/audit-logs", None),
    ],
)
async def test_outsider_gets_404_for_every_organization_route(
    api: Api, method: str, suffix: str, body: dict[str, str] | None
) -> None:
    victim = await api.register("owner@acme.io", organization="Acme")
    outsider = await api.register("owner@evil.io", organization="Evil")

    response = await outsider.client.request(
        method, f"{ORGS}/{victim.org_id}{suffix}", json=body, headers=outsider.headers
    )

    # 404, not 403: an outsider cannot even confirm the organization exists.
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "not_found"
    unchanged = await victim.get(f"{ORGS}/{victim.org_id}")
    assert unchanged.json()["name"] == "Acme"
    assert len((await victim.get(f"{ORGS}/{victim.org_id}/members")).json()) == 1


async def test_nonexistent_and_foreign_organizations_look_the_same(api: Api) -> None:
    victim = await api.register("owner@acme.io")
    outsider = await api.register("owner@evil.io", organization="Evil")

    foreign = (await outsider.get(f"{ORGS}/{victim.org_id}")).json()
    missing = (await outsider.get(f"{ORGS}/{uuid.uuid4()}")).json()
    foreign.pop("request_id")
    missing.pop("request_id")
    assert foreign == missing


async def test_member_of_another_tenant_cannot_be_addressed_through_own_organization(
    api: Api,
) -> None:
    """IDs from tenant B are meaningless inside tenant A's URLs."""
    victim = await api.register("owner@acme.io", organization="Acme")
    attacker = await api.register("owner@evil.io", organization="Evil")
    victim_member_id = (await victim.get(f"{ORGS}/{victim.org_id}/members")).json()[0]["id"]

    base = f"{ORGS}/{attacker.org_id}/members/{victim_member_id}"
    assert (await attacker.patch(base, json={"role": "viewer"})).status_code == 404
    assert (await attacker.delete(base)).status_code == 404

    members = (await victim.get(f"{ORGS}/{victim.org_id}/members")).json()
    assert [m["role"] for m in members] == ["owner"]


async def test_listings_contain_only_own_tenant_data(api: Api) -> None:
    acme = await api.register("owner@acme.io", organization="Acme")
    evil = await api.register("owner@evil.io", organization="Evil")
    await acme.post(f"{ORGS}/{acme.org_id}/invitations", json={"email": "secret-hire@acme.io"})

    assert [org["name"] for org in (await evil.get(ORGS)).json()] == ["Evil"]
    members = (await evil.get(f"{ORGS}/{evil.org_id}/members")).json()
    assert [m["email"] for m in members] == ["owner@evil.io"]

    audit = (await evil.get(f"{ORGS}/{evil.org_id}/audit-logs")).text
    assert "acme" not in audit.lower()
    assert acme.org_id not in audit


# --- database layer: row-level security -----------------------------------------------


async def _two_tenants_with_projects(
    api: Api, app_engine: AsyncEngine
) -> tuple[uuid.UUID, uuid.UUID]:
    acme = await api.register("owner@acme.io", organization="Acme")
    evil = await api.register("owner@evil.io", organization="Evil")
    org_a, org_b = uuid.UUID(acme.org_id), uuid.UUID(evil.org_id)
    async with AsyncSession(app_engine) as session:
        for org, names in ((org_a, ["web", "api"]), (org_b, ["evil-shop"])):
            async with session.begin():
                await set_current_org(session, org)
                repository = ProjectRepository(session, org)
                for name in names:
                    await repository.add(Project(organization_id=org, name=name, slug=name))
    return org_a, org_b


async def test_application_role_cannot_bypass_row_level_security(app_engine: AsyncEngine) -> None:
    async with app_engine.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).one()
    assert (row.rolsuper, row.rolbypassrls) == (False, False)


async def test_every_tenant_table_has_forced_row_level_security(
    owner_engine: AsyncEngine,
) -> None:
    protected = [*tenant_table_names(), "organizations", "audit_logs"]
    async with owner_engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class"
                    " WHERE relnamespace = 'public'::regnamespace AND relname = ANY(:names)"
                ),
                {"names": protected},
            )
        ).all()
        policies = (
            await connection.execute(text("SELECT DISTINCT tablename FROM pg_policies"))
        ).scalars()
        with_policy = set(policies)

    assert {row.relname for row in rows} == set(protected), "a tenant table is missing"
    unprotected = [
        row.relname for row in rows if not (row.relrowsecurity and row.relforcerowsecurity)
    ]
    assert unprotected == []
    assert set(protected) <= with_policy


async def test_unfiltered_sql_only_returns_the_current_tenants_rows(
    api: Api, app_engine: AsyncEngine
) -> None:
    org_a, org_b = await _two_tenants_with_projects(api, app_engine)
    query = text("SELECT name FROM projects ORDER BY name")  # deliberately no WHERE

    async with AsyncSession(app_engine) as session:
        async with session.begin():
            await set_current_org(session, org_a)
            assert list((await session.execute(query)).scalars()) == ["api", "web"]
        async with session.begin():
            await set_current_org(session, org_b)
            assert list((await session.execute(query)).scalars()) == ["evil-shop"]
        async with session.begin():
            # No tenant context at all: nothing is visible.
            assert list((await session.execute(query)).scalars()) == []


async def test_tenant_context_does_not_leak_across_transactions_on_a_connection(
    api: Api, app_engine: AsyncEngine
) -> None:
    org_a, _ = await _two_tenants_with_projects(api, app_engine)
    async with app_engine.connect() as connection:
        async with connection.begin():
            await connection.execute(
                text("SELECT set_config('app.current_org', :org, true)"), {"org": str(org_a)}
            )
            assert (await connection.execute(text("SELECT count(*) FROM projects"))).scalar() == 2
        # Same physical connection, next transaction: the setting is gone.
        async with connection.begin():
            assert (await connection.execute(text("SELECT count(*) FROM projects"))).scalar() == 0


async def test_cannot_write_into_or_modify_another_tenant(
    api: Api, app_engine: AsyncEngine, owner_engine: AsyncEngine
) -> None:
    org_a, org_b = await _two_tenants_with_projects(api, app_engine)

    async with AsyncSession(app_engine) as session:
        await set_current_org(session, org_a)
        with pytest.raises(DBAPIError) as excinfo:
            await session.execute(
                text(
                    "INSERT INTO projects (id, organization_id, name, slug)"
                    " VALUES (gen_random_uuid(), :org, 'planted', 'planted')"
                ),
                {"org": str(org_b)},
            )
        assert sqlstate(excinfo.value) == INSUFFICIENT_PRIVILEGE
        await session.rollback()

        await set_current_org(session, org_a)
        updated = await session.execute(text("UPDATE projects SET name = 'defaced'"))
        deleted = await session.execute(
            text("DELETE FROM projects WHERE organization_id = :org"), {"org": str(org_b)}
        )
        await session.commit()
    assert updated.rowcount == 2  # type: ignore[attr-defined]  # only tenant A's own rows
    assert deleted.rowcount == 0  # type: ignore[attr-defined]

    async with owner_engine.connect() as connection:
        names = (
            await connection.execute(
                text("SELECT name FROM projects WHERE organization_id = :org"), {"org": str(org_b)}
            )
        ).scalars()
        assert list(names) == ["evil-shop"]


async def test_repository_is_scoped_to_its_organization(api: Api, app_engine: AsyncEngine) -> None:
    org_a, org_b = await _two_tenants_with_projects(api, app_engine)
    async with AsyncSession(app_engine) as session:
        await set_current_org(session, org_a)
        repository = ProjectRepository(session, org_a)
        assert await repository.count() == 2
        assert {project.name for project in await repository.list()} == {"web", "api"}

        with pytest.raises(ValueError, match="different organization"):
            await repository.add(Project(organization_id=org_b, name="x", slug="x"))

        # Even a repository built for the wrong tenant is stopped by the database.
        assert await ProjectRepository(session, org_b).count() == 0


async def test_audit_log_is_append_only_for_the_application(
    api: Api, app_engine: AsyncEngine
) -> None:
    account = await api.register("owner@acme.io")
    for statement in ("UPDATE audit_logs SET action = 'tampered'", "DELETE FROM audit_logs"):
        async with AsyncSession(app_engine) as session:
            await set_current_org(session, uuid.UUID(account.org_id))
            with pytest.raises(DBAPIError) as excinfo:
                await session.execute(text(statement))
            assert sqlstate(excinfo.value) == INSUFFICIENT_PRIVILEGE
