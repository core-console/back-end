"""Protected PostgreSQL coverage for the Finance Overview read model."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.app import create_app
from core_console.config import AuthMode, Environment, Settings
from core_console.modules.users.models import User
from core_console.resources import get_application_resources

pytestmark = pytest.mark.anyio


@asynccontextmanager
async def _finance_client(
    *,
    database_url: str,
    actor: User,
    statement_log: list[str] | None = None,
) -> AsyncIterator[AsyncClient]:
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
        resources = get_application_resources(app)
        database = resources.database

        def record_statement(
            _connection: Any,
            _cursor: Any,
            statement: str,
            _parameters: Any,
            _context: Any,
            _executemany: bool,
        ) -> None:
            if statement_log is not None:
                statement_log.append(statement)

        if statement_log is not None:
            assert database is not None
            event.listen(database.engine.sync_engine, "before_cursor_execute", record_statement)
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                yield client
        finally:
            if statement_log is not None:
                assert database is not None
                event.remove(database.engine.sync_engine, "before_cursor_execute", record_statement)


async def test_overview_keeps_an_empty_owned_ledger_readable(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = User(
        identity_issuer="https://identity.example.test/finance-overview",
        identity_subject="empty-ledger",
        status="active",
    )
    postgres_session.add(actor)
    await postgres_session.commit()

    statement_log: list[str] = []
    async with _finance_client(
        database_url=postgres_database_url,
        actor=actor,
        statement_log=statement_log,
    ) as client:
        ledger = await client.post("/api/finance/ledgers", json={"name": "Empty"})
        statement_log.clear()
        overview = await client.get(
            f"/api/finance/ledgers/{ledger.json()['id']}/overview",
            params={"month": "2026-02"},
        )

    assert overview.status_code == HTTPStatus.OK
    assert overview.json() == {
        "ledger": ledger.json(),
        "month": "2026-02",
        "accounts": [],
        "financialPositionByCurrency": [],
        "monthSummaryByCurrency": [],
        "days": [],
    }
    finance_statements = [statement for statement in statement_log if "finance_" in statement]
    assert len(finance_statements) == 2
    assert all(
        statement.lstrip().startswith(("SELECT", "WITH")) for statement in finance_statements
    )
    assert all("FOR UPDATE" not in statement for statement in finance_statements)


async def _create_account(
    client: AsyncClient,
    *,
    ledger_id: str,
    name: str,
    nature: str,
    currency: str,
    opening_balance: str,
) -> dict[str, Any]:
    response = await client.post(
        f"/api/finance/ledgers/{ledger_id}/accounts",
        json={
            "name": name,
            "nature": nature,
            "currency": currency,
            "openingBalance": {"amount": opening_balance, "currency": currency},
            "trackingStartDate": "2020-01-01",
        },
    )
    assert response.status_code == HTTPStatus.CREATED
    return cast(dict[str, Any], response.json())


async def _create_ordinary(
    client: AsyncClient,
    *,
    ledger_id: str,
    account_id: str,
    kind: str,
    currency: str,
    amount: str,
    transaction_date: str,
) -> dict[str, Any]:
    response = await client.post(
        f"/api/finance/ledgers/{ledger_id}/transactions",
        json={
            "kind": kind,
            "accountId": account_id,
            "transactionDate": transaction_date,
            "economicAmount": {"amount": amount, "currency": currency},
            "categoryAllocations": [{"amount": {"amount": amount, "currency": currency}}],
        },
    )
    assert response.status_code == HTTPStatus.CREATED
    return cast(dict[str, Any], response.json())


async def test_overview_returns_present_account_relative_position_and_zero_month_groups(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = User(
        identity_issuer="https://identity.example.test/finance-overview",
        identity_subject="present-position",
        status="active",
    )
    postgres_session.add(actor)
    await postgres_session.commit()

    statement_log: list[str] = []
    async with _finance_client(
        database_url=postgres_database_url,
        actor=actor,
        statement_log=statement_log,
    ) as client:
        ledger = await client.post("/api/finance/ledgers", json={"name": "Position"})
        ledger_id = ledger.json()["id"]
        cash = await _create_account(
            client,
            ledger_id=ledger_id,
            name="Cash",
            nature="asset",
            currency="CNY",
            opening_balance="100",
        )
        card = await _create_account(
            client,
            ledger_id=ledger_id,
            name="Card",
            nature="liability",
            currency="CNY",
            opening_balance="40",
        )
        dollars = await _create_account(
            client,
            ledger_id=ledger_id,
            name="Dollars",
            nature="asset",
            currency="USD",
            opening_balance="-5",
        )
        yen_debt = await _create_account(
            client,
            ledger_id=ledger_id,
            name="Yen debt",
            nature="liability",
            currency="JPY",
            opening_balance="-3",
        )
        await _create_ordinary(
            client,
            ledger_id=ledger_id,
            account_id=cash["id"],
            kind="income",
            currency="CNY",
            amount="10",
            transaction_date="2027-01-01",
        )
        archived = await client.post(
            f"/api/finance/ledgers/{ledger_id}/accounts/{card['id']}/archive"
        )
        statement_log.clear()
        overview = await client.get(
            f"/api/finance/ledgers/{ledger_id}/overview",
            params={"month": "2026-02"},
        )

    assert overview.status_code == HTTPStatus.OK
    finance_statements = [statement for statement in statement_log if "finance_" in statement]
    assert len(finance_statements) == 2
    assert all(
        statement.lstrip().startswith(("SELECT", "WITH")) for statement in finance_statements
    )
    assert all("FOR UPDATE" not in statement for statement in finance_statements)
    payload = overview.json()
    assert payload["accounts"] == [
        {**cash, "currentBalance": {"amount": "110.00", "currency": "CNY"}},
        dollars,
        yen_debt,
        archived.json(),
    ]
    assert payload["financialPositionByCurrency"] == [
        {
            "currency": "CNY",
            "assetTotal": {"amount": "110.00", "currency": "CNY"},
            "liabilityTotal": {"amount": "40.00", "currency": "CNY"},
            "netPosition": {"amount": "70.00", "currency": "CNY"},
        },
        {
            "currency": "JPY",
            "assetTotal": {"amount": "0", "currency": "JPY"},
            "liabilityTotal": {"amount": "-3", "currency": "JPY"},
            "netPosition": {"amount": "3", "currency": "JPY"},
        },
        {
            "currency": "USD",
            "assetTotal": {"amount": "-5.00", "currency": "USD"},
            "liabilityTotal": {"amount": "0.00", "currency": "USD"},
            "netPosition": {"amount": "-5.00", "currency": "USD"},
        },
    ]
    assert payload["monthSummaryByCurrency"] == [
        {
            "currency": "CNY",
            "income": {"amount": "0.00", "currency": "CNY"},
            "expense": {"amount": "0.00", "currency": "CNY"},
            "net": {"amount": "0.00", "currency": "CNY"},
        },
        {
            "currency": "JPY",
            "income": {"amount": "0", "currency": "JPY"},
            "expense": {"amount": "0", "currency": "JPY"},
            "net": {"amount": "0", "currency": "JPY"},
        },
        {
            "currency": "USD",
            "income": {"amount": "0.00", "currency": "USD"},
            "expense": {"amount": "0.00", "currency": "USD"},
            "net": {"amount": "0.00", "currency": "USD"},
        },
    ]
    assert payload["days"] == []


async def test_overview_separates_month_economics_and_counts_all_transaction_kinds(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = User(
        identity_issuer="https://identity.example.test/finance-overview",
        identity_subject="month-activity",
        status="active",
    )
    postgres_session.add(actor)
    await postgres_session.commit()

    async with _finance_client(database_url=postgres_database_url, actor=actor) as client:
        ledger = await client.post("/api/finance/ledgers", json={"name": "Activity"})
        ledger_id = ledger.json()["id"]
        cash = await _create_account(
            client,
            ledger_id=ledger_id,
            name="Cash",
            nature="asset",
            currency="CNY",
            opening_balance="0",
        )
        savings = await _create_account(
            client,
            ledger_id=ledger_id,
            name="Savings",
            nature="asset",
            currency="CNY",
            opening_balance="0",
        )
        dollars = await _create_account(
            client,
            ledger_id=ledger_id,
            name="Dollar card",
            nature="liability",
            currency="USD",
            opening_balance="0",
        )
        await _create_account(
            client,
            ledger_id=ledger_id,
            name="Yen",
            nature="asset",
            currency="JPY",
            opening_balance="0",
        )
        await _create_ordinary(
            client,
            ledger_id=ledger_id,
            account_id=cash["id"],
            kind="income",
            currency="CNY",
            amount="10",
            transaction_date="2024-02-01",
        )
        await _create_ordinary(
            client,
            ledger_id=ledger_id,
            account_id=cash["id"],
            kind="expense",
            currency="CNY",
            amount="4",
            transaction_date="2024-02-29",
        )
        await _create_ordinary(
            client,
            ledger_id=ledger_id,
            account_id=dollars["id"],
            kind="income",
            currency="USD",
            amount="7",
            transaction_date="2024-02-29",
        )
        transfer = await client.post(
            f"/api/finance/ledgers/{ledger_id}/transactions",
            json={
                "kind": "internalTransfer",
                "sourceAccountId": cash["id"],
                "destinationAccountId": savings["id"],
                "amount": {"amount": "3", "currency": "CNY"},
                "transactionDate": "2024-02-29",
            },
        )
        assert transfer.status_code == HTTPStatus.CREATED
        context = await client.get(
            f"/api/finance/ledgers/{ledger_id}/accounts/{savings['id']}/balance-adjustment-context",
            params={"transactionDate": "2024-02-29"},
        )
        adjustment = await client.post(
            f"/api/finance/ledgers/{ledger_id}/balance-adjustments",
            json={
                "accountId": savings["id"],
                "transactionDate": "2024-02-29",
                "expectedDerivedBalance": context.json()["derivedComparisonBalance"],
                "expectedAccountNature": context.json()["accountNature"],
                "targetBalance": {"amount": "5", "currency": "CNY"},
            },
        )
        assert adjustment.status_code == HTTPStatus.OK
        overview = await client.get(
            f"/api/finance/ledgers/{ledger_id}/overview",
            params={"month": "2024-02"},
        )

    assert overview.status_code == HTTPStatus.OK
    payload = overview.json()
    assert {item["name"]: item["currentBalance"] for item in payload["accounts"]} == {
        "Cash": {"amount": "3.00", "currency": "CNY"},
        "Dollar card": {"amount": "-7.00", "currency": "USD"},
        "Savings": {"amount": "5.00", "currency": "CNY"},
        "Yen": {"amount": "0", "currency": "JPY"},
    }
    assert payload["financialPositionByCurrency"] == [
        {
            "currency": "CNY",
            "assetTotal": {"amount": "8.00", "currency": "CNY"},
            "liabilityTotal": {"amount": "0.00", "currency": "CNY"},
            "netPosition": {"amount": "8.00", "currency": "CNY"},
        },
        {
            "currency": "JPY",
            "assetTotal": {"amount": "0", "currency": "JPY"},
            "liabilityTotal": {"amount": "0", "currency": "JPY"},
            "netPosition": {"amount": "0", "currency": "JPY"},
        },
        {
            "currency": "USD",
            "assetTotal": {"amount": "0.00", "currency": "USD"},
            "liabilityTotal": {"amount": "-7.00", "currency": "USD"},
            "netPosition": {"amount": "7.00", "currency": "USD"},
        },
    ]
    assert payload["monthSummaryByCurrency"] == [
        {
            "currency": "CNY",
            "income": {"amount": "10.00", "currency": "CNY"},
            "expense": {"amount": "4.00", "currency": "CNY"},
            "net": {"amount": "6.00", "currency": "CNY"},
        },
        {
            "currency": "JPY",
            "income": {"amount": "0", "currency": "JPY"},
            "expense": {"amount": "0", "currency": "JPY"},
            "net": {"amount": "0", "currency": "JPY"},
        },
        {
            "currency": "USD",
            "income": {"amount": "7.00", "currency": "USD"},
            "expense": {"amount": "0.00", "currency": "USD"},
            "net": {"amount": "7.00", "currency": "USD"},
        },
    ]
    assert payload["days"] == [
        {
            "date": "2024-02-01",
            "transactionCount": 1,
            "transactionCountByKind": {
                "income": 1,
                "expense": 0,
                "internalTransfer": 0,
                "balanceAdjustment": 0,
            },
            "activityByCurrency": [
                {
                    "currency": "CNY",
                    "income": {"amount": "10.00", "currency": "CNY"},
                    "expense": {"amount": "0.00", "currency": "CNY"},
                    "net": {"amount": "10.00", "currency": "CNY"},
                    "transactionCount": 1,
                }
            ],
        },
        {
            "date": "2024-02-29",
            "transactionCount": 4,
            "transactionCountByKind": {
                "income": 1,
                "expense": 1,
                "internalTransfer": 1,
                "balanceAdjustment": 1,
            },
            "activityByCurrency": [
                {
                    "currency": "CNY",
                    "income": {"amount": "0.00", "currency": "CNY"},
                    "expense": {"amount": "4.00", "currency": "CNY"},
                    "net": {"amount": "-4.00", "currency": "CNY"},
                    "transactionCount": 3,
                },
                {
                    "currency": "USD",
                    "income": {"amount": "7.00", "currency": "USD"},
                    "expense": {"amount": "0.00", "currency": "USD"},
                    "net": {"amount": "7.00", "currency": "USD"},
                    "transactionCount": 1,
                },
            ],
        },
    ]


async def test_overview_uses_owned_ledger_scope_and_exact_required_month(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = User(
        identity_issuer="https://identity.example.test/finance-overview",
        identity_subject="owned-scope",
        status="active",
    )
    other = User(
        identity_issuer="https://identity.example.test/finance-overview",
        identity_subject="foreign-scope",
        status="active",
    )
    postgres_session.add_all([actor, other])
    await postgres_session.commit()

    async with _finance_client(database_url=postgres_database_url, actor=other) as client:
        foreign_ledger = await client.post("/api/finance/ledgers", json={"name": "Private"})
        foreign_account = await _create_account(
            client,
            ledger_id=foreign_ledger.json()["id"],
            name="Private",
            nature="asset",
            currency="USD",
            opening_balance="999",
        )
        await _create_ordinary(
            client,
            ledger_id=foreign_ledger.json()["id"],
            account_id=foreign_account["id"],
            kind="income",
            currency="USD",
            amount="111",
            transaction_date="2026-08-01",
        )

    async with _finance_client(database_url=postgres_database_url, actor=actor) as client:
        owned_ledger = await client.post("/api/finance/ledgers", json={"name": "Owned"})
        await _create_account(
            client,
            ledger_id=owned_ledger.json()["id"],
            name="Owned cash",
            nature="asset",
            currency="CNY",
            opening_balance="1",
        )
        owned_overview = await client.get(
            f"/api/finance/ledgers/{owned_ledger.json()['id']}/overview",
            params={"month": "2026-08"},
        )
        maximum_month = await client.get(
            f"/api/finance/ledgers/{owned_ledger.json()['id']}/overview",
            params={"month": "9999-12"},
        )
        missing = await client.get(
            "/api/finance/ledgers/00000000-0000-0000-0000-000000000001/overview",
            params={"month": "2026-08"},
        )
        foreign = await client.get(
            f"/api/finance/ledgers/{foreign_ledger.json()['id']}/overview",
            params={"month": "2026-08"},
        )
        invalid = [
            await client.get(
                f"/api/finance/ledgers/{foreign_ledger.json()['id']}/overview",
                params={"month": value},
            )
            for value in ("0000-01", "2026-8", "2026-13", "2026-08-01")
        ]
        missing_month = await client.get(
            f"/api/finance/ledgers/{foreign_ledger.json()['id']}/overview"
        )

    assert missing.status_code == HTTPStatus.NOT_FOUND
    assert foreign.status_code == HTTPStatus.NOT_FOUND
    assert [item["name"] for item in owned_overview.json()["accounts"]] == ["Owned cash"]
    assert [item["currency"] for item in owned_overview.json()["financialPositionByCurrency"]] == [
        "CNY"
    ]
    assert owned_overview.json()["days"] == []
    assert maximum_month.status_code == HTTPStatus.OK
    assert maximum_month.json()["month"] == "9999-12"
    assert missing.json()["code"] == foreign.json()["code"] == "finance_ledger_not_found"
    for response in [*invalid, missing_month]:
        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["code"] == "validation_error"


async def test_overview_month_boundaries_reflect_replacement_and_deletion_current_state(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = User(
        identity_issuer="https://identity.example.test/finance-overview",
        identity_subject="replacement-deletion",
        status="active",
    )
    postgres_session.add(actor)
    await postgres_session.commit()

    async with _finance_client(database_url=postgres_database_url, actor=actor) as client:
        ledger = await client.post("/api/finance/ledgers", json={"name": "Current state"})
        ledger_id = ledger.json()["id"]
        account = await _create_account(
            client,
            ledger_id=ledger_id,
            name="Cash",
            nature="asset",
            currency="CNY",
            opening_balance="0",
        )
        income = await _create_ordinary(
            client,
            ledger_id=ledger_id,
            account_id=account["id"],
            kind="income",
            currency="CNY",
            amount="10",
            transaction_date="2026-12-31",
        )
        await _create_ordinary(
            client,
            ledger_id=ledger_id,
            account_id=account["id"],
            kind="expense",
            currency="CNY",
            amount="2",
            transaction_date="2027-01-01",
        )
        december_before = await client.get(
            f"/api/finance/ledgers/{ledger_id}/overview", params={"month": "2026-12"}
        )
        replaced = await client.put(
            f"/api/finance/ledgers/{ledger_id}/transactions/{income['id']}",
            json={
                "kind": "income",
                "accountId": account["id"],
                "transactionDate": "2027-01-01",
                "economicAmount": {"amount": "6", "currency": "CNY"},
                "categoryAllocations": [{"amount": {"amount": "6", "currency": "CNY"}}],
            },
        )
        december_after = await client.get(
            f"/api/finance/ledgers/{ledger_id}/overview", params={"month": "2026-12"}
        )
        january_after = await client.get(
            f"/api/finance/ledgers/{ledger_id}/overview", params={"month": "2027-01"}
        )
        deleted = await client.delete(
            f"/api/finance/ledgers/{ledger_id}/transactions/{income['id']}"
        )
        january_deleted = await client.get(
            f"/api/finance/ledgers/{ledger_id}/overview", params={"month": "2027-01"}
        )

    assert december_before.json()["days"][0]["date"] == "2026-12-31"
    assert december_before.json()["monthSummaryByCurrency"][0]["income"]["amount"] == "10.00"
    assert replaced.status_code == HTTPStatus.OK
    assert december_after.json()["days"] == []
    assert december_after.json()["monthSummaryByCurrency"][0]["income"]["amount"] == "0.00"
    assert january_after.json()["days"][0]["transactionCount"] == 2
    assert january_after.json()["monthSummaryByCurrency"][0]["net"]["amount"] == "4.00"
    assert deleted.status_code == HTTPStatus.NO_CONTENT
    assert january_deleted.json()["days"][0]["transactionCount"] == 1
    assert january_deleted.json()["monthSummaryByCurrency"][0]["net"]["amount"] == "-2.00"


async def test_overview_reflects_balance_adjustment_replacement_and_removal(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = User(
        identity_issuer="https://identity.example.test/finance-overview",
        identity_subject="adjustment-aftermath",
        status="active",
    )
    postgres_session.add(actor)
    await postgres_session.commit()

    async with _finance_client(database_url=postgres_database_url, actor=actor) as client:
        ledger = await client.post("/api/finance/ledgers", json={"name": "Adjustments"})
        ledger_id = ledger.json()["id"]
        account = await _create_account(
            client,
            ledger_id=ledger_id,
            name="Cash",
            nature="asset",
            currency="CNY",
            opening_balance="10",
        )
        created = await client.post(
            f"/api/finance/ledgers/{ledger_id}/balance-adjustments",
            json={
                "accountId": account["id"],
                "transactionDate": "2026-04-10",
                "expectedDerivedBalance": {"amount": "10.00", "currency": "CNY"},
                "expectedAccountNature": "asset",
                "targetBalance": {"amount": "15.00", "currency": "CNY"},
            },
        )
        transaction_id = created.json()["transaction"]["id"]
        before = await client.get(
            f"/api/finance/ledgers/{ledger_id}/overview", params={"month": "2026-04"}
        )
        replaced = await client.put(
            f"/api/finance/ledgers/{ledger_id}/balance-adjustments/{transaction_id}",
            json={
                "accountId": account["id"],
                "transactionDate": "2026-04-11",
                "expectedDerivedBalance": {"amount": "10.00", "currency": "CNY"},
                "expectedAccountNature": "asset",
                "targetBalance": {"amount": "7.00", "currency": "CNY"},
            },
        )
        after_replace = await client.get(
            f"/api/finance/ledgers/{ledger_id}/overview", params={"month": "2026-04"}
        )
        removed = await client.put(
            f"/api/finance/ledgers/{ledger_id}/balance-adjustments/{transaction_id}",
            json={
                "accountId": account["id"],
                "transactionDate": "2026-04-11",
                "expectedDerivedBalance": {"amount": "10.00", "currency": "CNY"},
                "expectedAccountNature": "asset",
                "targetBalance": {"amount": "10.00", "currency": "CNY"},
            },
        )
        after_remove = await client.get(
            f"/api/finance/ledgers/{ledger_id}/overview", params={"month": "2026-04"}
        )

    assert created.status_code == HTTPStatus.OK
    assert before.json()["accounts"][0]["currentBalance"]["amount"] == "15.00"
    assert before.json()["days"][0]["transactionCountByKind"]["balanceAdjustment"] == 1
    assert before.json()["monthSummaryByCurrency"][0]["net"]["amount"] == "0.00"
    assert replaced.status_code == HTTPStatus.OK
    assert replaced.json()["transaction"]["id"] == transaction_id
    assert after_replace.json()["accounts"][0]["currentBalance"]["amount"] == "7.00"
    assert [item["date"] for item in after_replace.json()["days"]] == ["2026-04-11"]
    assert after_replace.json()["financialPositionByCurrency"][0]["netPosition"]["amount"] == (
        "7.00"
    )
    assert removed.json() == {"outcome": "removed", "transaction": None}
    assert after_remove.json()["accounts"][0]["currentBalance"]["amount"] == "10.00"
    assert after_remove.json()["days"] == []
    assert after_remove.json()["monthSummaryByCurrency"][0]["net"]["amount"] == "0.00"
