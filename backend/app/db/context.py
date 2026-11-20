"""Per-transaction identity used by PostgreSQL row-level security.

The policies created in the migrations compare each row's `organization_id`
with these settings. They are transaction-local (`set_config(..., true)`), so
a pooled connection can never carry one request's identity into the next.
After a commit they are gone: set them again before running further queries.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_SET = text("SELECT set_config(:name, :value, true)")


async def set_current_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    await session.execute(_SET, {"name": "app.current_user_id", "value": str(user_id)})


async def set_current_org(session: AsyncSession, organization_id: uuid.UUID) -> None:
    await session.execute(_SET, {"name": "app.current_org", "value": str(organization_id)})


async def set_invite_token_hash(session: AsyncSession, token_hash: str) -> None:
    """Makes exactly the invitation with this token hash visible to the transaction."""
    await session.execute(_SET, {"name": "app.invite_token_hash", "value": token_hash})
