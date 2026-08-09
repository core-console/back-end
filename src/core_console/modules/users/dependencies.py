"""FastAPI identity and current-user dependencies."""

from http import HTTPStatus
from typing import Annotated, Literal, cast

from fastapi import Depends, Request
from psycopg import OperationalError as PsycopgOperationalError
from psycopg.errors import ConnectionTimeout
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.database.dependencies import get_session
from core_console.modules.users.identity import CurrentUser, ExternalIdentity
from core_console.modules.users.queries import get_user_by_identity
from core_console.problems import ApplicationProblem
from core_console.resources import get_application_resources

_DATABASE_UNAVAILABLE_SQLSTATES = frozenset({"57P01", "57P02", "57P03"})


def is_database_unavailable(error: InterfaceError | OperationalError) -> bool:
    """Identify connection and server-availability failures from psycopg."""

    if error.connection_invalidated:
        return True
    if not isinstance(error.orig, PsycopgOperationalError):
        return False

    sqlstate = error.orig.sqlstate
    initial_connection_failure = (
        isinstance(error, OperationalError)
        and sqlstate is None
        and error.statement is None
        and error.params is None
    )
    return (
        isinstance(error.orig, ConnectionTimeout)
        or initial_connection_failure
        or (sqlstate is not None and sqlstate.startswith("08"))
        or sqlstate in _DATABASE_UNAVAILABLE_SQLSTATES
    )


def get_external_identity(request: Request) -> ExternalIdentity:
    """Construct the fixed development identity from validated settings."""

    settings = get_application_resources(request.app).settings
    return ExternalIdentity(
        issuer=settings.dev_identity_issuer,
        subject=settings.dev_identity_subject,
    )


async def get_current_user(
    identity: Annotated[ExternalIdentity, Depends(get_external_identity)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CurrentUser | None:
    """Resolve a local user by the complete external identity."""

    try:
        user = await get_user_by_identity(
            session,
            identity_issuer=identity.issuer,
            identity_subject=identity.subject,
        )
    except (InterfaceError, OperationalError) as exc:
        if not is_database_unavailable(exc):
            raise
        raise ApplicationProblem(
            status=HTTPStatus.SERVICE_UNAVAILABLE,
            title="Service Unavailable",
            detail="PostgreSQL is not available.",
            code="database_unavailable",
        ) from None

    if user is None:
        return None
    return CurrentUser(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        email=user.email,
        status=cast(Literal["active", "disabled"], user.status),
    )


def require_active_user(
    user: Annotated[CurrentUser | None, Depends(get_current_user)],
) -> CurrentUser:
    """Require a provisioned local user whose status is active."""

    if user is None or user.status != "active":
        raise ApplicationProblem(
            status=HTTPStatus.FORBIDDEN,
            title="Forbidden",
            detail="Access is denied.",
            code="access_denied",
        )
    return user
