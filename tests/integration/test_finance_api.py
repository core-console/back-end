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


async def test_user_can_manage_account_lifecycle_with_account_relative_balances(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = _user("account-lifecycle")
    postgres_session.add(actor)
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        ledger = await client.post("/api/finance/ledgers", json={"name": "Personal"})
        ledger_id = ledger.json()["id"]
        liability = await client.post(
            f"/api/finance/ledgers/{ledger_id}/accounts",
            json={
                "name": "  Credit Card  ",
                "nature": "liability",
                "currency": "CNY",
                "openingBalance": {"amount": "250", "currency": "CNY"},
                "trackingStartDate": "2026-08-01",
            },
        )
        asset = await client.post(
            f"/api/finance/ledgers/{ledger_id}/accounts",
            json={
                "name": "Cash",
                "nature": "asset",
                "currency": "JPY",
                "openingBalance": {"amount": "100", "currency": "JPY"},
                "trackingStartDate": "2026-08-02",
            },
        )
        unchanged = await client.patch(
            f"/api/finance/ledgers/{ledger_id}/accounts/{liability.json()['id']}",
            json={},
        )
        wrong_currency = await client.patch(
            f"/api/finance/ledgers/{ledger_id}/accounts/{liability.json()['id']}",
            json={"openingBalance": {"amount": "250.00", "currency": "USD"}},
        )
        archived = await client.post(
            f"/api/finance/ledgers/{ledger_id}/accounts/{liability.json()['id']}/archive"
        )
        archived_again = await client.post(
            f"/api/finance/ledgers/{ledger_id}/accounts/{liability.json()['id']}/archive"
        )
        listed = await client.get(f"/api/finance/ledgers/{ledger_id}/accounts")
        updated = await client.patch(
            f"/api/finance/ledgers/{ledger_id}/accounts/{liability.json()['id']}",
            json={
                "name": "Card",
                "openingBalance": {"amount": "275.5", "currency": "CNY"},
                "trackingStartDate": "2026-07-31",
            },
        )
        unarchived = await client.post(
            f"/api/finance/ledgers/{ledger_id}/accounts/{liability.json()['id']}/unarchive"
        )
        unarchived_again = await client.post(
            f"/api/finance/ledgers/{ledger_id}/accounts/{liability.json()['id']}/unarchive"
        )

    assert liability.status_code == HTTPStatus.CREATED
    assert liability.json() == {
        "id": liability.json()["id"],
        "name": "Credit Card",
        "nature": "liability",
        "currency": "CNY",
        "openingBalance": {"amount": "250.00", "currency": "CNY"},
        "trackingStartDate": "2026-08-01",
        "currentBalance": {"amount": "250.00", "currency": "CNY"},
        "status": "active",
    }
    assert asset.status_code == HTTPStatus.CREATED
    assert unchanged.json() == liability.json()
    assert wrong_currency.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert wrong_currency.json()["code"] == "validation_error"
    assert archived.json()["status"] == "archived"
    assert archived_again.json() == archived.json()
    assert listed.json() == [asset.json(), archived.json()]
    assert updated.json() == {
        **liability.json(),
        "name": "Card",
        "openingBalance": {"amount": "275.50", "currency": "CNY"},
        "trackingStartDate": "2026-07-31",
        "currentBalance": {"amount": "275.50", "currency": "CNY"},
        "status": "archived",
    }
    assert unarchived.json()["status"] == "active"
    assert unarchived_again.json() == unarchived.json()


async def test_account_lookups_do_not_leak_across_ledger_or_owner_scope(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = _user("account-scope-actor")
    other_user = _user("account-scope-other")
    postgres_session.add_all([actor, other_user])
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        first_ledger = await client.post("/api/finance/ledgers", json={"name": "First"})
        second_ledger = await client.post("/api/finance/ledgers", json={"name": "Second"})
        account = await client.post(
            f"/api/finance/ledgers/{first_ledger.json()['id']}/accounts",
            json={
                "name": "Private",
                "nature": "asset",
                "currency": "USD",
                "openingBalance": {"amount": "1.00", "currency": "USD"},
                "trackingStartDate": "2026-08-01",
            },
        )

    async with finance_client(database_url=postgres_database_url, actor=other_user) as client:
        non_owned_ledger = await client.get(
            f"/api/finance/ledgers/{first_ledger.json()['id']}/accounts"
        )

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        wrong_ledger = await client.patch(
            f"/api/finance/ledgers/{second_ledger.json()['id']}/accounts/{account.json()['id']}",
            json={"name": "Leaked"},
        )
        missing_account = await client.patch(
            f"/api/finance/ledgers/{second_ledger.json()['id']}/accounts/{uuid4()}",
            json={"name": "Missing"},
        )

    assert non_owned_ledger.status_code == HTTPStatus.NOT_FOUND
    assert non_owned_ledger.json()["code"] == "finance_ledger_not_found"
    assert wrong_ledger.status_code == HTTPStatus.NOT_FOUND
    assert missing_account.status_code == HTTPStatus.NOT_FOUND
    assert wrong_ledger.json()["code"] == "finance_account_not_found"
    assert missing_account.json()["code"] == "finance_account_not_found"
    assert wrong_ledger.json()["detail"] == missing_account.json()["detail"]


async def test_account_list_order_uses_status_case_folded_name_and_identifier(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = _user("account-order")
    postgres_session.add(actor)
    await postgres_session.commit()

    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        ledger = await client.post("/api/finance/ledgers", json={"name": "Order"})
        ledger_id = ledger.json()["id"]
        created = []
        for name in ("beta", "Alpha", "alpha"):
            response = await client.post(
                f"/api/finance/ledgers/{ledger_id}/accounts",
                json={
                    "name": name,
                    "nature": "asset",
                    "currency": "CNY",
                    "openingBalance": {"amount": "0", "currency": "CNY"},
                    "trackingStartDate": "2026-08-01",
                },
            )
            created.append(response.json())
        archived = await client.post(
            f"/api/finance/ledgers/{ledger_id}/accounts/{created[0]['id']}/archive"
        )
        listed = await client.get(f"/api/finance/ledgers/{ledger_id}/accounts")

    active = sorted(created[1:], key=lambda account: account["id"])
    assert listed.json() == [*active, archived.json()]


def _user(subject: str) -> User:
    return User(
        identity_issuer="https://identity.example.test/finance-api",
        identity_subject=subject,
        status="active",
    )
