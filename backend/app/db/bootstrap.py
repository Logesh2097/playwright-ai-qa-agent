"""Creates the application's database role.

Run before migrations:   python -m app.db.bootstrap

Two roles are used so that row-level security actually applies:

- the *migration role* (``MIGRATION_DATABASE_URL``) owns the schema and runs DDL;
- the *application role* (``DATABASE_URL``) is a plain login role without
  ``SUPERUSER`` or ``BYPASSRLS``. The API and the worker connect as this role.

This script connects as the migration role and creates the application role
with the name and password taken from ``DATABASE_URL`` (or updates its password).
It is idempotent. If no separate migration URL is configured it does nothing.
"""

import asyncio
import logging
import sys

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.logging import configure_logging

logger = logging.getLogger(__name__)


def application_role(database_url: str) -> str:
    username = make_url(database_url).username
    if not username:
        raise ValueError("DATABASE_URL has no username")
    return username


async def ensure_application_role() -> bool:
    settings = get_settings()
    if not settings.migration_database_url:
        logger.warning("db_bootstrap_skipped", extra={"reason": "no MIGRATION_DATABASE_URL"})
        return False

    app_url = make_url(settings.database_url)
    owner_url = make_url(settings.migration_database_url)
    role = application_role(settings.database_url)
    if role == owner_url.username:
        logger.warning("db_bootstrap_skipped", extra={"reason": "same role for both URLs"})
        return False
    if not app_url.password:
        raise ValueError("DATABASE_URL has no password")

    # hide_parameters: a failing statement must not print the password into logs.
    engine = create_async_engine(
        settings.migration_database_url, poolclass=NullPool, hide_parameters=True
    )
    try:
        async with engine.begin() as connection:
            exists = await connection.scalar(
                text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
            )
            # Role DDL cannot take bind parameters; format() quotes both values server-side.
            statement = await connection.scalar(
                text(
                    "SELECT format('%s ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE"
                    " NOBYPASSRLS PASSWORD %L',"
                    " CAST(:verb AS text), CAST(:role AS text), CAST(:password AS text))"
                ),
                {
                    "verb": "ALTER" if exists else "CREATE",
                    "role": role,
                    "password": app_url.password,
                },
            )
            try:
                await connection.exec_driver_sql(str(statement))
            except Exception:
                # The statement text contains the password, so the original error
                # (which quotes the statement) is deliberately not chained.
                raise RuntimeError(f"could not create or update database role {role!r}") from None
            await connection.exec_driver_sql(
                str(
                    await connection.scalar(
                        text(
                            "SELECT format('GRANT CONNECT ON DATABASE %I TO %I',"
                            " CAST(:db AS text), CAST(:role AS text))"
                        ),
                        {"db": owner_url.database, "role": role},
                    )
                )
            )
    finally:
        await engine.dispose()
    logger.info("db_role_ready", extra={"role": role, "newly_created": not exists})
    return True


def main() -> int:
    configure_logging(get_settings().log_level)
    asyncio.run(ensure_application_role())
    return 0


if __name__ == "__main__":
    sys.exit(main())
