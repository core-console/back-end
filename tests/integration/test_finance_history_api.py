"""Protected PostgreSQL coverage for Finance Transaction history."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient, Response
from pydantic import SecretStr
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.app import create_app
from core_console.config import AuthMode, Environment, Settings
from core_console.modules.finance.models import (
    FinanceAccountMovement,
    FinanceCategoryAllocation,
    FinanceTransaction,
)
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


async def test_history_pages_same_date_transactions_without_duplicates_or_omissions(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = User(
        identity_issuer="https://identity.example.test/finance-history",
        identity_subject="same-date-order",
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
        ledger = await client.post("/api/finance/ledgers", json={"name": "History"})
        ledger_id = ledger.json()["id"]
        empty = await client.get(f"/api/finance/ledgers/{ledger_id}/transactions")
        account = await client.post(
            f"/api/finance/ledgers/{ledger_id}/accounts",
            json={
                "name": "Cash",
                "nature": "asset",
                "currency": "CNY",
                "openingBalance": {"amount": "0", "currency": "CNY"},
                "trackingStartDate": "2026-08-01",
            },
        )
        created_ids: list[str] = []
        for index in range(5):
            created = await client.post(
                f"/api/finance/ledgers/{ledger_id}/transactions",
                json={
                    "kind": "income",
                    "accountId": account.json()["id"],
                    "transactionDate": "2026-08-21",
                    "economicAmount": {"amount": str(index + 1), "currency": "CNY"},
                    "categoryAllocations": [
                        {
                            "amount": {"amount": str(index + 1), "currency": "CNY"},
                            "categoryId": None,
                        }
                    ],
                },
            )
            assert created.status_code == HTTPStatus.CREATED
            created_ids.append(created.json()["id"])

        seen_ids: list[str] = []
        cursor: str | None = None
        page_sizes: list[int] = []
        while True:
            statement_log.clear()
            params = {"pageSize": "2"}
            if cursor is not None:
                params["cursor"] = cursor
            page = await client.get(
                f"/api/finance/ledgers/{ledger_id}/transactions",
                params=params,
            )
            assert page.status_code == HTTPStatus.OK
            finance_statements = [
                statement for statement in statement_log if "finance_" in statement
            ]
            assert len(finance_statements) == 2
            assert all(
                statement.lstrip().startswith(("SELECT", "WITH"))
                for statement in finance_statements
            )
            assert all("FOR UPDATE" not in statement for statement in finance_statements)
            payload = page.json()
            page_sizes.append(len(payload["items"]))
            seen_ids.extend(item["id"] for item in payload["items"])
            cursor = payload["nextCursor"]
            if cursor is None:
                break

    assert empty.json() == {"items": [], "nextCursor": None}
    assert page_sizes == [2, 2, 1]
    assert seen_ids == [str(value) for value in sorted(map(UUID, created_ids), reverse=True)]
    assert len(seen_ids) == len(set(seen_ids)) == 5


async def test_history_filters_all_kinds_archived_references_and_owned_resources(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = User(
        identity_issuer="https://identity.example.test/finance-history",
        identity_subject="filters-and-kinds",
        status="active",
    )
    other_user = User(
        identity_issuer="https://identity.example.test/finance-history",
        identity_subject="foreign-ledger-owner",
        status="active",
    )
    postgres_session.add_all([actor, other_user])
    await postgres_session.commit()

    async with _finance_client(
        database_url=postgres_database_url,
        actor=other_user,
    ) as other_client:
        foreign_owned_ledger = await other_client.post(
            "/api/finance/ledgers", json={"name": "Foreign Owner"}
        )

    async with _finance_client(database_url=postgres_database_url, actor=actor) as client:
        ledger = await client.post("/api/finance/ledgers", json={"name": "Owned"})
        other_ledger = await client.post("/api/finance/ledgers", json={"name": "Other"})
        ledger_id = ledger.json()["id"]
        other_ledger_id = other_ledger.json()["id"]
        cash = await _create_account(client, ledger_id=ledger_id, name="Cash")
        card = await _create_account(client, ledger_id=ledger_id, name="Card")
        foreign_account = await _create_account(client, ledger_id=other_ledger_id, name="Private")
        food = await client.post(
            f"/api/finance/ledgers/{ledger_id}/categories", json={"name": "Food"}
        )
        foreign_category = await client.post(
            f"/api/finance/ledgers/{other_ledger_id}/categories",
            json={"name": "Private"},
        )
        income = await _create_ordinary(
            client,
            ledger_id=ledger_id,
            kind="income",
            account_id=cash["id"],
            transaction_date="2026-08-24",
            category_id=food.json()["id"],
        )
        expense = await _create_ordinary(
            client,
            ledger_id=ledger_id,
            kind="expense",
            account_id=cash["id"],
            transaction_date="2026-08-23",
        )
        transfer = await client.post(
            f"/api/finance/ledgers/{ledger_id}/transactions",
            json={
                "kind": "internalTransfer",
                "sourceAccountId": cash["id"],
                "destinationAccountId": card["id"],
                "amount": {"amount": "3", "currency": "CNY"},
                "transactionDate": "2026-08-22",
            },
        )
        adjustment_context = await client.get(
            f"/api/finance/ledgers/{ledger_id}/accounts/{card['id']}/balance-adjustment-context",
            params={"transactionDate": "2026-08-21"},
        )
        context = adjustment_context.json()
        adjustment = await client.post(
            f"/api/finance/ledgers/{ledger_id}/balance-adjustments",
            json={
                "accountId": card["id"],
                "transactionDate": "2026-08-21",
                "expectedDerivedBalance": context["derivedComparisonBalance"],
                "expectedAccountNature": context["accountNature"],
                "targetBalance": {"amount": "5", "currency": "CNY"},
            },
        )
        await client.post(f"/api/finance/ledgers/{ledger_id}/accounts/{cash['id']}/archive")
        await client.post(
            f"/api/finance/ledgers/{ledger_id}/categories/{food.json()['id']}/archive"
        )

        all_history = await _list_history(client, ledger_id)
        selected_day = await _list_history(
            client,
            ledger_id,
            fromDate="2026-08-23",
            toDate="2026-08-23",
        )
        from_history = await _list_history(client, ledger_id, fromDate="2026-08-23")
        to_history = await _list_history(client, ledger_id, toDate="2026-08-22")
        cash_history = await _list_history(client, ledger_id, accountId=cash["id"])
        card_history = await _list_history(client, ledger_id, accountId=card["id"])
        category_history = await _list_history(client, ledger_id, categoryId=food.json()["id"])
        uncategorized_history = await _list_history(client, ledger_id, uncategorized="true")
        kind_histories = {
            kind: await _list_history(client, ledger_id, kind=kind)
            for kind in ("income", "expense", "internalTransfer", "balanceAdjustment")
        }
        exact_boundary = await client.get(
            f"/api/finance/ledgers/{ledger_id}/transactions",
            params={"fromDate": "2026-08-23", "pageSize": "2"},
        )
        conjunctive_history = await _list_history(
            client,
            ledger_id,
            fromDate="2026-08-24",
            toDate="2026-08-24",
            accountId=cash["id"],
            kind="income",
            categoryId=food.json()["id"],
        )
        foreign_account_filter = await client.get(
            f"/api/finance/ledgers/{ledger_id}/transactions",
            params={"accountId": foreign_account["id"]},
        )
        foreign_category_filter = await client.get(
            f"/api/finance/ledgers/{ledger_id}/transactions",
            params={"categoryId": foreign_category.json()["id"]},
        )
        missing_ledger = await client.get(f"/api/finance/ledgers/{uuid4()}/transactions")
        foreign_owned_ledger_response = await client.get(
            f"/api/finance/ledgers/{foreign_owned_ledger.json()['id']}/transactions"
        )

    adjustment_id = adjustment.json()["transaction"]["id"]
    assert [item["id"] for item in all_history] == [
        income.json()["id"],
        expense.json()["id"],
        transfer.json()["id"],
        adjustment_id,
    ]
    assert [item["kind"] for item in all_history] == [
        "income",
        "expense",
        "internalTransfer",
        "balanceAdjustment",
    ]
    assert all_history[0]["account"]["status"] == "archived"
    assert all_history[0]["categoryAllocations"][0]["category"]["status"] == "archived"
    assert all_history[0]["economicAmount"] == {"amount": "10.00", "currency": "CNY"}
    assert all_history[1]["economicAmount"] == {"amount": "10.00", "currency": "CNY"}
    assert all_history[2]["sourceAmount"] == {"amount": "3.00", "currency": "CNY"}
    assert all_history[2]["destinationAmount"] == {"amount": "3.00", "currency": "CNY"}
    assert all_history[3]["correctionDelta"] == {"amount": "5.00", "currency": "CNY"}
    assert [item["id"] for item in selected_day] == [expense.json()["id"]]
    assert [item["id"] for item in from_history] == [income.json()["id"], expense.json()["id"]]
    assert [item["id"] for item in to_history] == [transfer.json()["id"], adjustment_id]
    assert [item["id"] for item in cash_history] == [
        income.json()["id"],
        expense.json()["id"],
        transfer.json()["id"],
    ]
    assert [item["id"] for item in card_history] == [transfer.json()["id"], adjustment_id]
    assert [item["id"] for item in category_history] == [income.json()["id"]]
    assert [item["id"] for item in uncategorized_history] == [expense.json()["id"]]
    assert {kind: [item["kind"] for item in items] for kind, items in kind_histories.items()} == {
        kind: [kind] for kind in kind_histories
    }
    assert len(exact_boundary.json()["items"]) == 2
    assert exact_boundary.json()["nextCursor"] is None
    assert [item["id"] for item in conjunctive_history] == [income.json()["id"]]
    assert foreign_account_filter.status_code == HTTPStatus.NOT_FOUND
    assert foreign_account_filter.json()["code"] == "finance_account_not_found"
    assert foreign_category_filter.status_code == HTTPStatus.NOT_FOUND
    assert foreign_category_filter.json()["code"] == "finance_category_not_found"
    assert missing_ledger.status_code == HTTPStatus.NOT_FOUND
    assert missing_ledger.json()["code"] == "finance_ledger_not_found"
    assert foreign_owned_ledger_response.status_code == HTTPStatus.NOT_FOUND
    assert foreign_owned_ledger_response.json()["code"] == "finance_ledger_not_found"


async def test_history_cursor_is_opaque_validated_and_bound_to_filters(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = User(
        identity_issuer="https://identity.example.test/finance-history",
        identity_subject="cursor-validation",
        status="active",
    )
    postgres_session.add(actor)
    await postgres_session.commit()

    async with _finance_client(database_url=postgres_database_url, actor=actor) as client:
        ledger = await client.post("/api/finance/ledgers", json={"name": "Cursor"})
        other_ledger = await client.post("/api/finance/ledgers", json={"name": "Other Cursor"})
        ledger_id = ledger.json()["id"]
        account = await _create_account(client, ledger_id=ledger_id, name="Cash")
        for day in (24, 23, 22):
            await _create_ordinary(
                client,
                ledger_id=ledger_id,
                kind="income",
                account_id=account["id"],
                transaction_date=f"2026-08-{day}",
            )
        first = await client.get(
            f"/api/finance/ledgers/{ledger_id}/transactions",
            params={"kind": "income", "pageSize": "2"},
        )
        cursor = first.json()["nextCursor"]
        continued_with_different_size = await client.get(
            f"/api/finance/ledgers/{ledger_id}/transactions",
            params={"kind": "income", "pageSize": "1", "cursor": cursor},
        )
        changed_filter = await client.get(
            f"/api/finance/ledgers/{ledger_id}/transactions",
            params={"kind": "expense", "pageSize": "2", "cursor": cursor},
        )
        malformed = await client.get(
            f"/api/finance/ledgers/{ledger_id}/transactions",
            params={"cursor": "not-a-cursor"},
        )
        tampered = await client.get(
            f"/api/finance/ledgers/{ledger_id}/transactions",
            params={"kind": "income", "pageSize": "2", "cursor": f"{cursor}!"},
        )
        other_ledger_reuse = await client.get(
            f"/api/finance/ledgers/{other_ledger.json()['id']}/transactions",
            params={"kind": "income", "pageSize": "2", "cursor": cursor},
        )

    assert cursor is not None
    assert continued_with_different_size.status_code == HTTPStatus.OK
    assert len(continued_with_different_size.json()["items"]) == 1
    assert continued_with_different_size.json()["nextCursor"] is None
    for response in (changed_filter, malformed, tampered, other_ledger_reuse):
        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
        assert response.json()["code"] == "validation_error"


async def test_history_reads_current_mutation_aftermath_without_writing(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    actor = User(
        identity_issuer="https://identity.example.test/finance-history",
        identity_subject="mutation-aftermath",
        status="active",
    )
    postgres_session.add(actor)
    await postgres_session.commit()

    async with _finance_client(database_url=postgres_database_url, actor=actor) as client:
        ledger = await client.post("/api/finance/ledgers", json={"name": "Current State"})
        ledger_id = ledger.json()["id"]
        cash = await _create_account(client, ledger_id=ledger_id, name="Cash")
        reserve = await _create_account(client, ledger_id=ledger_id, name="Reserve")
        kept = await _create_ordinary(
            client,
            ledger_id=ledger_id,
            kind="income",
            account_id=cash["id"],
            transaction_date="2026-08-20",
        )
        deleted = await _create_ordinary(
            client,
            ledger_id=ledger_id,
            kind="expense",
            account_id=cash["id"],
            transaction_date="2026-08-19",
        )
        replaced = await client.put(
            f"/api/finance/ledgers/{ledger_id}/transactions/{kept.json()['id']}",
            json={
                "kind": "income",
                "accountId": cash["id"],
                "transactionDate": "2026-08-25",
                "economicAmount": {"amount": "12", "currency": "CNY"},
                "categoryAllocations": [
                    {
                        "amount": {"amount": "12", "currency": "CNY"},
                        "categoryId": None,
                    }
                ],
                "note": "replacement",
            },
        )
        removed_ordinary = await client.delete(
            f"/api/finance/ledgers/{ledger_id}/transactions/{deleted.json()['id']}"
        )
        updated_adjustment = await _create_adjustment(
            client,
            ledger_id=ledger_id,
            account_id=reserve["id"],
            transaction_date="2026-08-18",
            target_amount="5",
        )
        removed_adjustment = await _create_adjustment(
            client,
            ledger_id=ledger_id,
            account_id=cash["id"],
            transaction_date="2026-08-17",
            target_amount="4",
        )
        updated_context = await client.get(
            f"/api/finance/ledgers/{ledger_id}/accounts/{reserve['id']}/balance-adjustment-context",
            params={
                "transactionDate": "2026-08-18",
                "replacingTransactionId": updated_adjustment["id"],
            },
        )
        updated = await client.put(
            f"/api/finance/ledgers/{ledger_id}/balance-adjustments/{updated_adjustment['id']}",
            json={
                "accountId": reserve["id"],
                "transactionDate": "2026-08-18",
                "expectedDerivedBalance": updated_context.json()["derivedComparisonBalance"],
                "expectedAccountNature": updated_context.json()["accountNature"],
                "targetBalance": {"amount": "7", "currency": "CNY"},
            },
        )
        removed_context = await client.get(
            f"/api/finance/ledgers/{ledger_id}/accounts/{cash['id']}/balance-adjustment-context",
            params={
                "transactionDate": "2026-08-17",
                "replacingTransactionId": removed_adjustment["id"],
            },
        )
        removed = await client.put(
            f"/api/finance/ledgers/{ledger_id}/balance-adjustments/{removed_adjustment['id']}",
            json={
                "accountId": cash["id"],
                "transactionDate": "2026-08-17",
                "expectedDerivedBalance": removed_context.json()["derivedComparisonBalance"],
                "expectedAccountNature": removed_context.json()["accountNature"],
                "targetBalance": removed_context.json()["derivedComparisonBalance"],
            },
        )
        accounts_before = await client.get(f"/api/finance/ledgers/{ledger_id}/accounts")
        categories_before = await client.get(f"/api/finance/ledgers/{ledger_id}/categories")
        counts_before = await _durable_counts(postgres_session)

        first_read = await _list_history(client, ledger_id)
        second_read = await _list_history(client, ledger_id, fromDate="2026-08-01")

        accounts_after = await client.get(f"/api/finance/ledgers/{ledger_id}/accounts")
        categories_after = await client.get(f"/api/finance/ledgers/{ledger_id}/categories")
        counts_after = await _durable_counts(postgres_session)

    assert replaced.status_code == HTTPStatus.OK
    assert replaced.json()["id"] == kept.json()["id"]
    assert removed_ordinary.status_code == HTTPStatus.NO_CONTENT
    assert updated.json()["outcome"] == "updated"
    assert updated.json()["transaction"]["id"] == updated_adjustment["id"]
    assert removed.json() == {"outcome": "removed", "transaction": None}
    assert [item["id"] for item in first_read] == [
        kept.json()["id"],
        updated_adjustment["id"],
    ]
    assert first_read == second_read
    assert first_read[0]["economicAmount"] == {"amount": "12.00", "currency": "CNY"}
    assert first_read[0]["note"] == "replacement"
    assert first_read[1]["correctionDelta"] == {"amount": "7.00", "currency": "CNY"}
    assert deleted.json()["id"] not in {item["id"] for item in first_read}
    assert removed_adjustment["id"] not in {item["id"] for item in first_read}
    assert counts_after == counts_before
    assert accounts_after.json() == accounts_before.json()
    assert categories_after.json() == categories_before.json()


async def _list_history(
    client: AsyncClient,
    ledger_id: str,
    **params: str,
) -> list[dict[str, Any]]:
    response = await client.get(
        f"/api/finance/ledgers/{ledger_id}/transactions",
        params=params,
    )
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["nextCursor"] is None
    return cast(list[dict[str, Any]], payload["items"])


async def _create_account(
    client: AsyncClient,
    *,
    ledger_id: str,
    name: str,
) -> dict[str, Any]:
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
    assert response.status_code == HTTPStatus.CREATED
    return cast(dict[str, Any], response.json())


async def _create_ordinary(
    client: AsyncClient,
    *,
    ledger_id: str,
    kind: str,
    account_id: str,
    transaction_date: str,
    category_id: str | None = None,
) -> Response:
    response = await client.post(
        f"/api/finance/ledgers/{ledger_id}/transactions",
        json={
            "kind": kind,
            "accountId": account_id,
            "transactionDate": transaction_date,
            "economicAmount": {"amount": "10", "currency": "CNY"},
            "categoryAllocations": [
                {
                    "amount": {"amount": "10", "currency": "CNY"},
                    "categoryId": category_id,
                }
            ],
        },
    )
    assert response.status_code == HTTPStatus.CREATED
    return response


async def _create_adjustment(
    client: AsyncClient,
    *,
    ledger_id: str,
    account_id: str,
    transaction_date: str,
    target_amount: str,
) -> dict[str, Any]:
    context = await client.get(
        f"/api/finance/ledgers/{ledger_id}/accounts/{account_id}/balance-adjustment-context",
        params={"transactionDate": transaction_date},
    )
    assert context.status_code == HTTPStatus.OK
    payload = context.json()
    response = await client.post(
        f"/api/finance/ledgers/{ledger_id}/balance-adjustments",
        json={
            "accountId": account_id,
            "transactionDate": transaction_date,
            "expectedDerivedBalance": payload["derivedComparisonBalance"],
            "expectedAccountNature": payload["accountNature"],
            "targetBalance": {"amount": target_amount, "currency": "CNY"},
        },
    )
    assert response.status_code == HTTPStatus.OK
    return cast(dict[str, Any], response.json()["transaction"])


async def _durable_counts(session: AsyncSession) -> tuple[int, int, int]:
    counts: list[int] = []
    for model in (FinanceTransaction, FinanceAccountMovement, FinanceCategoryAllocation):
        count = await session.scalar(select(func.count()).select_from(model))
        assert count is not None
        counts.append(count)
    return counts[0], counts[1], counts[2]
