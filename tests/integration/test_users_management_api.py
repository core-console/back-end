"""Real PostgreSQL coverage for the Users v1 management API."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from http import HTTPStatus
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.app import create_app
from core_console.config import AuthMode, Environment, Settings
from core_console.modules.users.models import User

pytestmark = pytest.mark.anyio


@asynccontextmanager
async def management_client(
    *,
    database_url: str,
    actor: User,
) -> AsyncIterator[AsyncClient]:
    """Call management routes as one active persisted local user."""

    app = create_app(
        Settings(
            environment=Environment.TEST,
            auth_mode=AuthMode.DEVELOPMENT,
            dev_identity_issuer=actor.identity_issuer,
            dev_identity_subject=actor.identity_subject,
            database_url=SecretStr(database_url),
            database_connect_timeout_seconds=0.1,
        )
    )
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


async def test_list_users_returns_all_users_in_stable_order(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    """An active local user can list management profiles with public statuses."""

    first_created = datetime(2026, 1, 1, tzinfo=UTC)
    second_created = datetime(2026, 1, 2, tzinfo=UTC)
    inactive_user = User(
        identity_issuer=" https://identity.example.test/tenant-b ",
        identity_subject=" inactive-subject ",
        username=None,
        display_name="Inactive User",
        email="inactive@example.test",
        status="disabled",
        created_at=first_created,
        updated_at=first_created,
    )
    actor = User(
        identity_issuer="https://identity.example.test/tenant-a",
        identity_subject="active-subject",
        username="active-user",
        display_name=None,
        email=None,
        status="active",
        created_at=second_created,
        updated_at=second_created,
    )
    postgres_session.add_all([actor, inactive_user])
    await postgres_session.commit()

    async with management_client(database_url=postgres_database_url, actor=actor) as client:
        response = await client.get("/api/users")

    assert response.status_code == HTTPStatus.OK
    assert response.json() == [
        {
            "id": str(inactive_user.id),
            "displayName": "Inactive User",
            "username": None,
            "email": "inactive@example.test",
            "identityIssuer": " https://identity.example.test/tenant-b ",
            "identitySubject": " inactive-subject ",
            "status": "inactive",
        },
        {
            "id": str(actor.id),
            "displayName": None,
            "username": "active-user",
            "email": None,
            "identityIssuer": "https://identity.example.test/tenant-a",
            "identitySubject": "active-subject",
            "status": "active",
        },
    ]


async def test_create_user_normalizes_profile_and_returns_active_user(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    """Creating a user trims input and always starts the user as active."""

    actor = User(
        identity_issuer="https://identity.example.test/actor",
        identity_subject="actor-subject",
        username="actor",
        status="active",
    )
    postgres_session.add(actor)
    await postgres_session.commit()

    async with management_client(database_url=postgres_database_url, actor=actor) as client:
        response = await client.post(
            "/api/users",
            json={
                "displayName": "  New User  ",
                "username": " \t ",
                "email": None,
                "identityIssuer": "  https://identity.example.test/new  ",
                "identitySubject": "  new-subject  ",
            },
        )

    assert response.status_code == HTTPStatus.CREATED
    assert response.json() == {
        "id": response.json()["id"],
        "displayName": "New User",
        "username": None,
        "email": None,
        "identityIssuer": "https://identity.example.test/new",
        "identitySubject": "new-subject",
        "status": "active",
    }


async def test_create_user_rejects_snake_case_request_fields(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    """Runtime validation accepts only the camelCase fields declared by OpenAPI."""

    actor = User(
        identity_issuer="https://identity.example.test/actor",
        identity_subject="actor-subject",
        display_name="Actor",
        status="active",
    )
    postgres_session.add(actor)
    await postgres_session.commit()

    async with management_client(database_url=postgres_database_url, actor=actor) as client:
        response = await client.post(
            "/api/users",
            json={
                "display_name": "Snake Case User",
                "username": None,
                "email": None,
                "identityIssuer": "https://identity.example.test/new",
                "identitySubject": "new-subject",
            },
        )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "validation_error"


async def test_create_user_rejects_an_empty_profile(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    """A local user must retain at least one meaningful profile field."""

    actor = User(
        identity_issuer="https://identity.example.test/actor",
        identity_subject="actor-subject",
        display_name="Actor",
        status="active",
    )
    postgres_session.add(actor)
    await postgres_session.commit()

    async with management_client(database_url=postgres_database_url, actor=actor) as client:
        response = await client.post(
            "/api/users",
            json={
                "displayName": "  ",
                "username": "\t",
                "email": None,
                "identityIssuer": "https://identity.example.test/new",
                "identitySubject": "new-subject",
            },
        )
        users_response = await client.get("/api/users")

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "validation_error"
    assert [user["id"] for user in users_response.json()] == [str(actor.id)]


async def test_create_user_rejects_a_blank_identity_component(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    """Identity components are trimmed but cannot normalize to blank."""

    actor = User(
        identity_issuer="https://identity.example.test/actor",
        identity_subject="actor-subject",
        display_name="Actor",
        status="active",
    )
    postgres_session.add(actor)
    await postgres_session.commit()

    async with management_client(database_url=postgres_database_url, actor=actor) as client:
        response = await client.post(
            "/api/users",
            json={
                "displayName": "New User",
                "username": None,
                "email": None,
                "identityIssuer": " \t ",
                "identitySubject": "new-subject",
            },
        )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "validation_error"


async def test_create_user_rejects_a_duplicate_identity_mapping(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    """The existing identity-pair constraint becomes a stable API conflict."""

    actor = User(
        identity_issuer="https://identity.example.test/actor",
        identity_subject="actor-subject",
        display_name="Actor",
        status="active",
    )
    postgres_session.add(actor)
    await postgres_session.commit()

    async with management_client(database_url=postgres_database_url, actor=actor) as client:
        response = await client.post(
            "/api/users",
            json={
                "displayName": "Duplicate",
                "username": None,
                "email": None,
                "identityIssuer": "  https://identity.example.test/actor  ",
                "identitySubject": "  actor-subject  ",
            },
        )

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "user_conflict"


async def test_update_user_distinguishes_omitted_fields_from_explicit_null(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    """PATCH preserves omitted fields and clears fields explicitly set to null."""

    actor = User(
        identity_issuer="https://identity.example.test/actor",
        identity_subject="actor-subject",
        display_name="Actor",
        status="active",
    )
    target = User(
        identity_issuer="https://identity.example.test/target",
        identity_subject="target-subject",
        display_name="Before",
        username="keep-me",
        email="clear-me@example.test",
        status="active",
    )
    postgres_session.add_all([actor, target])
    await postgres_session.commit()

    async with management_client(database_url=postgres_database_url, actor=actor) as client:
        response = await client.patch(
            f"/api/users/{target.id}",
            json={"displayName": "  After  ", "email": None},
        )

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "id": str(target.id),
        "displayName": "After",
        "username": "keep-me",
        "email": None,
        "identityIssuer": "https://identity.example.test/target",
        "identitySubject": "target-subject",
        "status": "active",
    }


async def test_update_user_rejects_an_empty_resulting_profile(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    """PATCH cannot clear the target's final meaningful profile field."""

    actor = User(
        identity_issuer="https://identity.example.test/actor",
        identity_subject="actor-subject",
        display_name="Actor",
        status="active",
    )
    target = User(
        identity_issuer="https://identity.example.test/target",
        identity_subject="target-subject",
        display_name="Only Field",
        status="active",
    )
    postgres_session.add_all([actor, target])
    await postgres_session.commit()

    async with management_client(database_url=postgres_database_url, actor=actor) as client:
        response = await client.patch(
            f"/api/users/{target.id}",
            json={"displayName": None},
        )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "validation_error"


async def test_deactivate_user_preserves_identity_mapping(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    """Deactivation changes only the local lifecycle state."""

    actor = User(
        identity_issuer="https://identity.example.test/actor",
        identity_subject="actor-subject",
        display_name="Actor",
        status="active",
    )
    target = User(
        identity_issuer="https://identity.example.test/target",
        identity_subject="target-subject",
        display_name="Target",
        status="active",
    )
    postgres_session.add_all([actor, target])
    await postgres_session.commit()

    async with management_client(database_url=postgres_database_url, actor=actor) as client:
        response = await client.post(f"/api/users/{target.id}/deactivate")

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "id": str(target.id),
        "displayName": "Target",
        "username": None,
        "email": None,
        "identityIssuer": "https://identity.example.test/target",
        "identitySubject": "target-subject",
        "status": "inactive",
    }


async def test_user_cannot_deactivate_self(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    """The lifecycle workflow owns the current-actor safety rule."""

    actor = User(
        identity_issuer="https://identity.example.test/actor",
        identity_subject="actor-subject",
        display_name="Actor",
        status="active",
    )
    postgres_session.add(actor)
    await postgres_session.commit()

    async with management_client(database_url=postgres_database_url, actor=actor) as client:
        response = await client.post(f"/api/users/{actor.id}/deactivate")

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "cannot_deactivate_self"


async def test_reactivate_user_returns_active_status(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    """A disabled local user can be returned to active service."""

    actor = User(
        identity_issuer="https://identity.example.test/actor",
        identity_subject="actor-subject",
        display_name="Actor",
        status="active",
    )
    target = User(
        identity_issuer="https://identity.example.test/target",
        identity_subject="target-subject",
        display_name="Target",
        status="disabled",
    )
    postgres_session.add_all([actor, target])
    await postgres_session.commit()

    async with management_client(database_url=postgres_database_url, actor=actor) as client:
        response = await client.post(f"/api/users/{target.id}/reactivate")

    assert response.status_code == HTTPStatus.OK
    assert response.json()["id"] == str(target.id)
    assert response.json()["status"] == "active"


@pytest.mark.parametrize(
    ("method", "path_suffix", "body"),
    (
        pytest.param("PATCH", "", {"displayName": "Missing"}, id="update"),
        pytest.param("POST", "/deactivate", None, id="deactivate"),
        pytest.param("POST", "/reactivate", None, id="reactivate"),
    ),
)
async def test_missing_user_returns_stable_not_found_problem(
    postgres_database_url: str,
    postgres_session: AsyncSession,
    method: str,
    path_suffix: str,
    body: dict[str, str] | None,
) -> None:
    """Every target-specific workflow shares the user-not-found contract."""

    actor = User(
        identity_issuer="https://identity.example.test/actor",
        identity_subject="actor-subject",
        display_name="Actor",
        status="active",
    )
    postgres_session.add(actor)
    await postgres_session.commit()

    async with management_client(database_url=postgres_database_url, actor=actor) as client:
        if body is None:
            response = await client.request(
                method,
                f"/api/users/{uuid4()}{path_suffix}",
            )
        else:
            response = await client.request(
                method,
                f"/api/users/{uuid4()}{path_suffix}",
                json=body,
            )

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "user_not_found"
