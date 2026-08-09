"""Dedicated queries for persisted users."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.users.models import User


async def list_users(session: AsyncSession) -> Sequence[User]:
    """Return all users in deterministic creation order."""

    statement = select(User).order_by(User.created_at, User.id)
    result = await session.scalars(statement)
    return result.all()


async def get_user_by_id(session: AsyncSession, *, user_id: UUID) -> User | None:
    """Find one local user by its stable internal identifier."""

    return await session.get(User, user_id)


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
