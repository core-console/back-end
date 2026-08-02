"""Dedicated queries for persisted users."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.users.models import User


async def get_user_by_identity(
    session: AsyncSession,
    *,
    identity_issuer: str,
    identity_subject: str,
) -> User | None:
    """Find one user by the complete external identity key."""

    statement = select(User).where(
        User.identity_issuer == identity_issuer,
        User.identity_subject == identity_subject,
    )
    result = await session.execute(statement)
    return result.scalar_one_or_none()
