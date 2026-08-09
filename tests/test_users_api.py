"""Current-user dependency and API contract tests."""

from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from httpx import AsyncClient
from psycopg import InterfaceError as PsycopgInterfaceError
from psycopg import OperationalError as PsycopgOperationalError
from psycopg import errors as psycopg_errors
from sqlalchemy.exc import InterfaceError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.database.dependencies import get_session
from core_console.modules.users.dependencies import get_current_user, get_external_identity
from core_console.modules.users.identity import CurrentUser, ExternalIdentity


@pytest.mark.anyio
async def test_external_identity_comes_only_from_server_settings(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    del client
    request = Request(
        {
            "type": "http",
            "app": app,
            "method": "GET",
            "path": "/api/me",
            "query_string": b"subject=attacker",
            "headers": [
                (b"authorization", b"Bearer attacker"),
                (b"cookie", b"identity=attacker"),
                (b"x-user-subject", b"attacker"),
            ],
        }
    )

    identity = get_external_identity(request)

    assert identity == ExternalIdentity(
        issuer="https://identity.example.test",
        subject="test-developer",
    )


@pytest.mark.anyio
async def test_active_user_can_get_own_public_profile(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    user_id = uuid4()
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=user_id,
        username=None,
        display_name="Local Developer",
        email=None,
        status="active",
    )

    response = await client.get("/api/me")

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "id": str(user_id),
        "username": None,
        "displayName": "Local Developer",
        "email": None,
    }


@pytest.mark.anyio
async def test_missing_and_disabled_users_receive_the_same_access_denied_problem(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    app.dependency_overrides[get_current_user] = lambda: None
    missing_response = await client.get("/api/me")

    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=uuid4(),
        username="disabled",
        display_name="Disabled User",
        email="disabled@example.test",
        status="disabled",
    )
    disabled_response = await client.get("/api/me")

    assert missing_response.status_code == HTTPStatus.FORBIDDEN
    assert missing_response.headers["content-type"].startswith("application/problem+json")
    assert (
        missing_response.json()
        == disabled_response.json()
        == {
            "type": "about:blank",
            "title": "Forbidden",
            "status": 403,
            "detail": "Access is denied.",
            "instance": "/api/me",
            "code": "access_denied",
        }
    )


@pytest.mark.anyio
async def test_missing_database_configuration_preserves_startup_contract(
    client: AsyncClient,
) -> None:
    response = await client.get("/api/me")

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "database_not_configured"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("failure", "expected_status", "expected_code"),
    (
        pytest.param(
            InterfaceError(
                "SELECT users",
                {},
                PsycopgInterfaceError("connection closed"),
                connection_invalidated=True,
            ),
            HTTPStatus.SERVICE_UNAVAILABLE,
            "database_unavailable",
            id="invalidated-interface-error",
        ),
        pytest.param(
            OperationalError(
                "SELECT users",
                {},
                psycopg_errors.ConnectionFailure("connection failure"),
            ),
            HTTPStatus.SERVICE_UNAVAILABLE,
            "database_unavailable",
            id="sqlstate-08",
        ),
        pytest.param(
            OperationalError("SELECT users", {}, psycopg_errors.AdminShutdown("shutdown")),
            HTTPStatus.SERVICE_UNAVAILABLE,
            "database_unavailable",
            id="sqlstate-57p01",
        ),
        pytest.param(
            OperationalError("SELECT users", {}, psycopg_errors.CrashShutdown("crash")),
            HTTPStatus.SERVICE_UNAVAILABLE,
            "database_unavailable",
            id="sqlstate-57p02",
        ),
        pytest.param(
            OperationalError("SELECT users", {}, psycopg_errors.CannotConnectNow("starting")),
            HTTPStatus.SERVICE_UNAVAILABLE,
            "database_unavailable",
            id="sqlstate-57p03",
        ),
        pytest.param(
            OperationalError(
                "SELECT users",
                {},
                psycopg_errors.ConnectionTimeout("connection timeout"),
            ),
            HTTPStatus.SERVICE_UNAVAILABLE,
            "database_unavailable",
            id="connection-timeout",
        ),
        pytest.param(
            OperationalError(
                None,
                None,
                PsycopgOperationalError("connection refused"),
                connection_invalidated=False,
            ),
            HTTPStatus.SERVICE_UNAVAILABLE,
            "database_unavailable",
            id="initial-connection-failure",
        ),
        pytest.param(
            InterfaceError(
                "SELECT users",
                {},
                PsycopgInterfaceError("cursor misuse"),
                connection_invalidated=False,
            ),
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "internal_error",
            id="non-invalidated-interface-error",
        ),
        pytest.param(
            OperationalError(
                "SELECT pg_sleep(10)",
                {},
                psycopg_errors.QueryCanceled("query canceled"),
            ),
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "internal_error",
            id="query-canceled",
        ),
        pytest.param(
            ProgrammingError("SELECT broken", {}, ValueError("invalid SQL")),
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "internal_error",
            id="programming-error",
        ),
        pytest.param(
            OperationalError(
                "UPDATE users",
                {},
                psycopg_errors.SerializationFailure("serialization failure"),
            ),
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "internal_error",
            id="non-availability-operational-error",
        ),
        pytest.param(
            OperationalError(
                "SELECT users",
                None,
                PsycopgOperationalError("unclassified operational failure"),
            ),
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "internal_error",
            id="statement-operational-error",
        ),
        pytest.param(
            RuntimeError("mapping defect"),
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "internal_error",
            id="runtime-error",
        ),
    ),
)
async def test_database_failure_is_classified_at_http_boundary(
    app: FastAPI,
    client: AsyncClient,
    failure: Exception,
    expected_status: HTTPStatus,
    expected_code: str,
) -> None:
    session = cast(AsyncSession, AsyncMock(spec=AsyncSession))
    cast(AsyncMock, session.execute).side_effect = failure

    async def failing_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = failing_session

    response = await client.get("/api/me")

    assert response.status_code == expected_status
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["status"] == expected_status
    assert response.json()["code"] == expected_code


@pytest.mark.anyio
async def test_management_database_failure_is_classified_after_actor_resolution(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    """Users workflows preserve the database-unavailable Problem Details contract."""

    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=uuid4(),
        username="actor",
        display_name=None,
        email=None,
        status="active",
    )
    session = cast(AsyncSession, AsyncMock(spec=AsyncSession))
    cast(AsyncMock, session.scalars).side_effect = OperationalError(
        None,
        None,
        PsycopgOperationalError("connection refused"),
        connection_invalidated=False,
    )

    async def failing_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = failing_session

    response = await client.get("/api/users")

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "database_unavailable"


@pytest.mark.anyio
async def test_unprotected_endpoints_do_not_resolve_current_user(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    resolve_current_user = AsyncMock(side_effect=AssertionError("must not resolve current user"))
    app.dependency_overrides[get_current_user] = resolve_current_user

    live_response = await client.get("/health/live")
    hello_response = await client.get("/api/helloWorld")
    docs_response = await client.get("/api/docs")

    assert live_response.status_code == HTTPStatus.OK
    assert hello_response.status_code == HTTPStatus.OK
    assert docs_response.status_code == HTTPStatus.OK
    resolve_current_user.assert_not_awaited()
