"""Schema-level guarantees: constraints, migrations, and infrastructure wiring."""

import os
import uuid
from collections.abc import AsyncIterator

import pytest
from alembic.script import ScriptDirectory
from arq import create_pool
from arq.connections import RedisSettings
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from alembic import command
from app.db.errors import (
    CHECK_VIOLATION,
    FOREIGN_KEY_VIOLATION,
    UNIQUE_VIOLATION,
    constraint_name,
    sqlstate,
)
from app.models import Base
from tests.conftest import Api, TestDatabase, alembic_config

pytestmark = pytest.mark.integration


@pytest.fixture
async def db(owner_engine: AsyncEngine, clean_state: None) -> AsyncIterator[AsyncConnection]:
    """Owner connection (no row-level security), rolled back after each test."""
    async with owner_engine.connect() as connection:
        transaction = await connection.begin()
        yield connection
        await transaction.rollback()


async def _org(db: AsyncConnection, slug: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    await db.execute(
        text("INSERT INTO organizations (id, name, slug) VALUES (:id, :name, :slug)"),
        {"id": org_id, "name": slug.title(), "slug": slug},
    )
    return org_id


async def _user(db: AsyncConnection, email: str) -> uuid.UUID:
    user_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO users (id, email, password_hash, full_name) VALUES (:id, :email, 'x', 'U')"
        ),
        {"id": user_id, "email": email},
    )
    return user_id


async def _project(db: AsyncConnection, org_id: uuid.UUID, slug: str = "web") -> uuid.UUID:
    project_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO projects (id, organization_id, name, slug) VALUES (:id, :org, :name, :slug)"
        ),
        {"id": project_id, "org": org_id, "name": slug.title(), "slug": slug},
    )
    return project_id


async def _violation(
    db: AsyncConnection, sql: str, **params: object
) -> tuple[str | None, str | None]:
    """Run a statement that must be rejected; return (sqlstate, constraint name)."""
    savepoint = await db.begin_nested()
    with pytest.raises(IntegrityError) as excinfo:
        await db.execute(text(sql), params)
    await savepoint.rollback()
    return sqlstate(excinfo.value), constraint_name(excinfo.value)


# --- constraints -------------------------------------------------------------------


async def test_user_email_is_unique_ignoring_case(db: AsyncConnection) -> None:
    await _user(db, "asha@acme.io")
    assert await _violation(
        db,
        "INSERT INTO users (id, email, password_hash, full_name)"
        " VALUES (gen_random_uuid(), 'ASHA@ACME.IO', 'x', 'U')",
    ) == (UNIQUE_VIOLATION, "uq_users_email")


async def test_membership_is_unique_per_user_and_role_is_checked(db: AsyncConnection) -> None:
    org, user = await _org(db, "acme"), await _user(db, "asha@acme.io")
    insert = (
        "INSERT INTO organization_members (id, organization_id, user_id, role, status)"
        " VALUES (gen_random_uuid(), :org, :user, :role, :status)"
    )
    await db.execute(text(insert), {"org": org, "user": user, "role": "owner", "status": "active"})

    assert await _violation(db, insert, org=org, user=user, role="viewer", status="active") == (
        UNIQUE_VIOLATION,
        "uq_organization_members_organization_id",
    )
    other = await _user(db, "b@acme.io")
    assert await _violation(db, insert, org=org, user=other, role="superuser", status="active") == (
        CHECK_VIOLATION,
        "ck_organization_members_role_valid",
    )
    # An active membership needs a user; only an invitation may have none.
    assert await _violation(db, insert, org=org, user=None, role="viewer", status="active") == (
        CHECK_VIOLATION,
        "ck_organization_members_user_required",
    )


async def test_membership_requires_existing_organization_and_user(db: AsyncConnection) -> None:
    state, _ = await _violation(
        db,
        "INSERT INTO organization_members (id, organization_id, user_id, role)"
        " VALUES (gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), 'viewer')",
    )
    assert state == FOREIGN_KEY_VIOLATION


async def test_slug_is_unique_among_active_rows_only(db: AsyncConnection) -> None:
    first = await _org(db, "acme")
    state, name = await _violation(
        db, "INSERT INTO organizations (id, name, slug) VALUES (gen_random_uuid(), 'A', 'ACME')"
    )
    assert (state, name) == (UNIQUE_VIOLATION, "uq_organizations_slug_active")

    # After a soft delete the slug can be used again.
    await db.execute(
        text("UPDATE organizations SET deleted_at = now() WHERE id = :id"), {"id": first}
    )
    await _org(db, "acme")

    org = await _org(db, "other")
    await _project(db, org, "web")
    state, name = await _violation(
        db,
        "INSERT INTO projects (id, organization_id, name, slug)"
        " VALUES (gen_random_uuid(), :org, 'Web', 'web')",
        org=org,
    )
    assert (state, name) == (UNIQUE_VIOLATION, "uq_projects_org_slug_active")
    # The same project slug in a different organization is fine.
    await _project(db, await _org(db, "third"), "web")


async def test_child_rows_must_belong_to_their_parents_organization(db: AsyncConnection) -> None:
    """Composite foreign keys: an application cannot point at another tenant's project."""
    org_a, org_b = await _org(db, "acme"), await _org(db, "evil")
    project_a = await _project(db, org_a)
    insert_app = (
        "INSERT INTO applications (id, organization_id, project_id, name, base_url)"
        " VALUES (:id, :org, :project, 'shop', 'https://acme.io')"
    )

    assert await _violation(db, insert_app, id=uuid.uuid4(), org=org_b, project=project_a) == (
        FOREIGN_KEY_VIOLATION,
        "fk_applications_project_id_projects",
    )

    app_a = uuid.uuid4()
    await db.execute(text(insert_app), {"id": app_a, "org": org_a, "project": project_a})
    insert_env = (
        "INSERT INTO environments (id, organization_id, application_id, name, kind, base_url)"
        " VALUES (:id, :org, :app, 'staging', 'staging', 'https://staging.acme.io')"
    )
    assert await _violation(db, insert_env, id=uuid.uuid4(), org=org_b, app=app_a) == (
        FOREIGN_KEY_VIOLATION,
        "fk_environments_application_id_applications",
    )

    env_a = uuid.uuid4()
    await db.execute(text(insert_env), {"id": env_a, "org": org_a, "app": app_a})
    insert_execution = (
        "INSERT INTO test_executions (id, organization_id, project_id, environment_id)"
        " VALUES (gen_random_uuid(), :org, :project, :env)"
    )
    state, _ = await _violation(db, insert_execution, org=org_b, project=project_a, env=env_a)
    assert state == FOREIGN_KEY_VIOLATION
    await db.execute(text(insert_execution), {"org": org_a, "project": project_a, "env": env_a})


async def test_status_and_kind_columns_reject_unknown_values(db: AsyncConnection) -> None:
    org = await _org(db, "acme")
    project = await _project(db, org)
    app = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO applications (id, organization_id, project_id, name, base_url)"
            " VALUES (:id, :org, :project, 'shop', 'https://acme.io')"
        ),
        {"id": app, "org": org, "project": project},
    )
    assert await _violation(
        db,
        "INSERT INTO environments (id, organization_id, application_id, name, kind, base_url)"
        " VALUES (gen_random_uuid(), :org, :app, 'x', 'sandbox', 'https://x.io')",
        org=org,
        app=app,
    ) == (CHECK_VIOLATION, "ck_environments_kind_valid")


async def test_allowed_origin_must_be_a_plain_host(db: AsyncConnection) -> None:
    org = await _org(db, "acme")
    project = await _project(db, org)
    app, env = uuid.uuid4(), uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO applications (id, organization_id, project_id, name, base_url)"
            " VALUES (:app, :org, :project, 'shop', 'https://acme.io')"
        ),
        {"app": app, "org": org, "project": project},
    )
    await db.execute(
        text(
            "INSERT INTO environments (id, organization_id, application_id, name, kind, base_url)"
            " VALUES (:env, :org, :app, 'staging', 'staging', 'https://s.acme.io')"
        ),
        {"env": env, "org": org, "app": app},
    )
    insert = (
        "INSERT INTO environment_allowed_origins"
        " (id, organization_id, environment_id, scheme, host, port)"
        " VALUES (gen_random_uuid(), :org, :env, :scheme, :host, :port)"
    )
    for host in ("*.acme.io", "acme.io/admin", "user@acme.io", "acme .io", ""):
        assert await _violation(
            db, insert, org=org, env=env, scheme="https", host=host, port=443
        ) == (
            CHECK_VIOLATION,
            "ck_environment_allowed_origins_host_plain",
        )
    _, name = await _violation(db, insert, org=org, env=env, scheme="ftp", host="acme.io", port=21)
    assert name == "ck_environment_allowed_origins_scheme_valid"
    _, name = await _violation(db, insert, org=org, env=env, scheme="https", host="a.io", port=0)
    assert name == "ck_environment_allowed_origins_port_range"
    await db.execute(
        text(insert), {"org": org, "env": env, "scheme": "https", "host": "s.acme.io", "port": 443}
    )


async def test_assertion_step_must_record_the_source_of_its_expected_value(
    db: AsyncConnection,
) -> None:
    org = await _org(db, "acme")
    project = await _project(db, org)
    app, case, version = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO applications (id, organization_id, project_id, name, base_url)"
            " VALUES (:app, :org, :project, 'shop', 'https://acme.io')"
        ),
        {"app": app, "org": org, "project": project},
    )
    await db.execute(
        text(
            "INSERT INTO test_cases (id, organization_id, project_id, application_id, key, title, type)"
            " VALUES (:case, :org, :project, :app, 'TC-1', 'Login', 'ui')"
        ),
        {"case": case, "org": org, "project": project, "app": app},
    )
    await db.execute(
        text(
            "INSERT INTO test_case_versions (id, organization_id, test_case_id, version, title, category)"
            " VALUES (:version, :org, :case, 1, 'Login', 'positive')"
        ),
        {"version": version, "org": org, "case": case},
    )
    insert = (
        "INSERT INTO test_steps"
        " (id, organization_id, test_case_version_id, position, action, is_assertion, expected_source)"
        " VALUES (gen_random_uuid(), :org, :version, :position, 'assert_text', true, :source)"
    )
    assert await _violation(db, insert, org=org, version=version, position=1, source=None) == (
        CHECK_VIOLATION,
        "ck_test_steps_assertion_has_source",
    )
    await db.execute(
        text(insert), {"org": org, "version": version, "position": 1, "source": "user_specified"}
    )
    _, name = await _violation(
        db, insert, org=org, version=version, position=1, source="user_specified"
    )
    assert name == "uq_test_steps_test_case_version_id"

    # An expected result is either known and present, or unknown and absent: never a
    # result labelled unknown, never a known source without a result.
    for source, result in (("unknown", "Probably fine"), ("plan_objective", None)):
        _, constraint = await _violation(
            db,
            "UPDATE test_case_versions SET expected_source = :source, expected_result = :result",
            source=source,
            result=result,
        )
        assert constraint == "ck_test_case_versions_expected_result_matches_source"
    await db.execute(
        text(
            "UPDATE test_case_versions SET expected_source = 'human_specified',"
            " expected_result = 'Shows an error'"
        )
    )
    for column, value in (("category", "odd"), ("severity", "blocker"), ("source", "robot")):
        _, constraint = await _violation(
            db, f"UPDATE test_case_versions SET {column} = :value", value=value
        )
        assert constraint == f"ck_test_case_versions_{column}_valid"

    # The case can point at its current version (circular foreign key).
    await db.execute(
        text("UPDATE test_cases SET current_version_id = :version WHERE id = :case"),
        {"version": version, "case": case},
    )


async def test_every_table_has_a_primary_key_and_created_at(owner_engine: AsyncEngine) -> None:
    async with owner_engine.connect() as connection:
        tables = set(
            (
                await connection.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            ).scalars()
        )
    expected = {table.name for table in Base.metadata.sorted_tables}
    assert expected <= tables
    assert len(expected) == 39
    for table in Base.metadata.sorted_tables:
        assert table.primary_key.columns, table.name
        assert "created_at" in table.columns or table.name == "test_suite_cases", table.name


# --- migrations ----------------------------------------------------------------------


def test_database_is_at_the_latest_revision_and_matches_the_models(
    test_database: TestDatabase,
) -> None:
    config = alembic_config(test_database.owner_url)
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head == "0009"
    # Raises if the models and the migrated schema have drifted apart.
    command.check(config)


def test_migrations_downgrade_to_empty_and_upgrade_again(test_database: TestDatabase) -> None:
    config = alembic_config(test_database.owner_url)
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    command.check(config)


# --- infrastructure (Phase 1) ----------------------------------------------------------


async def test_readyz_with_real_dependencies(api: Api) -> None:
    response = await api.client().get("/readyz")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ready"


async def test_worker_executes_ping_job() -> None:
    """Uses the development Redis database, where the running worker listens."""
    pool = await create_pool(RedisSettings.from_dsn(os.environ["REDIS_URL"]))
    try:
        job = await pool.enqueue_job("ping")
        assert job is not None
        assert await job.result(timeout=20) == "pong"
    finally:
        await pool.aclose()
