"""Real PostgreSQL coverage for the Finance Ledger HTTP slice."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
async def finance_client(
    *,
    database_url: str,
    actor: User,
) -> AsyncIterator[AsyncClient]:
    """Call Finance routes as one persisted active Local User."""

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


async def test_user_can_create_list_and_rename_only_their_own_ledgers(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = _user("actor")
    other_user = _user("other")
    postgres_session.add_all([actor, other_user])
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        beta_response = await client.post("/api/finance/ledgers", json={"name": "  Beta  "})
        alpha_response = await client.post("/api/finance/ledgers", json={"name": "alpha"})
        zebra_response = await client.post("/api/finance/ledgers", json={"name": "zebra"})
        umlaut_response = await client.post("/api/finance/ledgers", json={"name": "Äpfel"})
        listed_response = await client.get("/api/finance/ledgers")
        unchanged_response = await client.patch(
            f"/api/finance/ledgers/{beta_response.json()['id']}",
            json={},
        )
        renamed_response = await client.patch(
            f"/api/finance/ledgers/{beta_response.json()['id']}",
            json={"name": "  Personal  "},
        )

    async with finance_client(database_url=postgres_database_url, actor=other_user) as client:
        other_create_response = await client.post(
            "/api/finance/ledgers",
            json={"name": "ALPHA"},
        )
        other_listed_response = await client.get("/api/finance/ledgers")

    assert beta_response.status_code == HTTPStatus.CREATED
    assert beta_response.json()["name"] == "Beta"
    assert alpha_response.status_code == HTTPStatus.CREATED
    assert listed_response.json() == [
        alpha_response.json(),
        beta_response.json(),
        zebra_response.json(),
        umlaut_response.json(),
    ]
    assert unchanged_response.json() == beta_response.json()
    assert renamed_response.json() == {
        "id": beta_response.json()["id"],
        "name": "Personal",
    }
    assert other_create_response.status_code == HTTPStatus.CREATED
    assert other_listed_response.json() == [other_create_response.json()]


async def test_case_folded_ledger_names_conflict_only_for_the_same_owner(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = _user("casefold")
    postgres_session.add(actor)
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        created_response = await client.post(
            "/api/finance/ledgers",
            json={"name": "Straße"},
        )
        duplicate_response = await client.post(
            "/api/finance/ledgers",
            json={"name": "STRASSE"},
        )
        work_response = await client.post("/api/finance/ledgers", json={"name": "Work"})
        rename_response = await client.patch(
            f"/api/finance/ledgers/{work_response.json()['id']}",
            json={"name": "strasse"},
        )

    assert created_response.status_code == HTTPStatus.CREATED
    for response in (duplicate_response, rename_response):
        assert response.status_code == HTTPStatus.CONFLICT
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["code"] == "finance_ledger_name_conflict"


async def test_missing_and_non_owned_ledgers_have_the_same_safe_result(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = _user("lookup-actor")
    other_user = _user("lookup-other")
    postgres_session.add_all([actor, other_user])
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=other_user) as client:
        other_ledger = await client.post("/api/finance/ledgers", json={"name": "Private"})

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        non_owned_response = await client.patch(
            f"/api/finance/ledgers/{other_ledger.json()['id']}",
            json={"name": "Disclosed"},
        )
        missing_response = await client.patch(
            f"/api/finance/ledgers/{uuid4()}",
            json={"name": "Missing"},
        )

    assert non_owned_response.status_code == HTTPStatus.NOT_FOUND
    assert missing_response.status_code == HTTPStatus.NOT_FOUND
    assert non_owned_response.json()["code"] == "finance_ledger_not_found"
    assert missing_response.json()["code"] == "finance_ledger_not_found"
    assert non_owned_response.json()["detail"] == missing_response.json()["detail"]


@pytest.mark.parametrize(
    "body",
    (
        {"name": " \t "},
        {"name": "界" * 101},
        {"name": None},
        {"name": "Personal", "default": True},
    ),
)
async def test_create_ledger_rejects_invalid_or_unknown_fields(
    postgres_database_url: str,
    postgres_session: AsyncSession,
    body: dict[str, object],
) -> None:
    actor = _user(uuid4().hex)
    postgres_session.add(actor)
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        response = await client.post("/api/finance/ledgers", json=body)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "validation_error"


async def test_malformed_ledger_id_uses_validation_problem(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = _user("malformed-id")
    postgres_session.add(actor)
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        response = await client.patch(
            "/api/finance/ledgers/not-a-uuid",
            json={"name": "Personal"},
        )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "validation_error"


def _user(subject: str) -> User:
    return User(
        identity_issuer="https://identity.example.test/finance-api",
        identity_subject=subject,
        status="active",
    )
