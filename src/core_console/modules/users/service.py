"""Users v1 management workflows."""

from typing import TypedDict
from uuid import UUID

from psycopg.errors import UniqueViolation
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.users.models import User
from core_console.modules.users.queries import get_user_by_id


class UserProfileUpdate(TypedDict, total=False):
    """Profile values present in one PATCH request."""

    display_name: str | None
    username: str | None
    email: str | None


class InvalidUserRequestError(ValueError):
    """The requested user state violates a Users v1 business rule."""


class UserConflictError(Exception):
    """The requested external identity is already mapped."""


class UserNotFoundError(Exception):
    """The requested local user does not exist."""


class CannotDeactivateSelfError(Exception):
    """The current actor attempted to deactivate their own user."""


def _normalize_profile_value(value: str | None) -> str | None:
    """Trim a profile value and collapse blank content to null."""

    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _normalize_identity_value(value: str) -> str:
    """Trim one required identity component."""

    normalized = value.strip()
    if not normalized:
        raise InvalidUserRequestError("Identity issuer and subject must not be blank.")
    return normalized


def _require_profile(
    *,
    display_name: str | None,
    username: str | None,
    email: str | None,
) -> None:
    """Require at least one meaningful local profile value."""

    if display_name is None and username is None and email is None:
        raise InvalidUserRequestError(
            "At least one of displayName, username, or email must have content."
        )


async def create_managed_user(
    session: AsyncSession,
    *,
    display_name: str | None,
    username: str | None,
    email: str | None,
    identity_issuer: str,
    identity_subject: str,
) -> User:
    """Create an active local user with normalized input."""

    normalized_display_name = _normalize_profile_value(display_name)
    normalized_username = _normalize_profile_value(username)
    normalized_email = _normalize_profile_value(email)
    _require_profile(
        display_name=normalized_display_name,
        username=normalized_username,
        email=normalized_email,
    )
    user = User(
        display_name=normalized_display_name,
        username=normalized_username,
        email=normalized_email,
        identity_issuer=_normalize_identity_value(identity_issuer),
        identity_subject=_normalize_identity_value(identity_subject),
        status="active",
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if (
            isinstance(exc.orig, UniqueViolation)
            and exc.orig.diag.constraint_name == "uq_users_identity_issuer_subject"
        ):
            raise UserConflictError from None
        raise
    return user


async def update_managed_user(
    session: AsyncSession,
    *,
    user_id: UUID,
    updates: UserProfileUpdate,
) -> User:
    """Apply normalized partial profile changes to one local user."""

    user = await get_user_by_id(session, user_id=user_id)
    if user is None:
        raise UserNotFoundError

    display_name = (
        _normalize_profile_value(updates["display_name"])
        if "display_name" in updates
        else user.display_name
    )
    username = (
        _normalize_profile_value(updates["username"]) if "username" in updates else user.username
    )
    email = _normalize_profile_value(updates["email"]) if "email" in updates else user.email
    _require_profile(display_name=display_name, username=username, email=email)

    user.display_name = display_name
    user.username = username
    user.email = email
    await session.commit()
    return user


async def deactivate_managed_user(
    session: AsyncSession,
    *,
    user_id: UUID,
    actor_id: UUID,
) -> User:
    """Disable one local user while protecting the current actor."""

    if user_id == actor_id:
        raise CannotDeactivateSelfError

    user = await get_user_by_id(session, user_id=user_id)
    if user is None:
        raise UserNotFoundError

    user.status = "disabled"
    await session.commit()
    return user


async def reactivate_managed_user(
    session: AsyncSession,
    *,
    user_id: UUID,
) -> User:
    """Return one disabled local user to active service."""

    user = await get_user_by_id(session, user_id=user_id)
    if user is None:
        raise UserNotFoundError

    user.status = "active"
    await session.commit()
    return user
