"""Acceptance gaps shared by the completed five-operation protocol."""

import asyncio
import json
from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance.models import (
    FinanceAccount,
    FinanceAccountMovement,
    FinanceCategory,
    FinanceCategoryAllocation,
    FinanceLedger,
    FinanceTransaction,
)
from core_console.modules.finance.submission_models import FinanceSubmission
from core_console.modules.users.models import User
from integration.finance_submission_helpers import submission_headers
from integration.test_finance_api import finance_client
from integration.test_finance_transaction_submissions import transaction_scope as transaction_scope

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "operation",
    ["ledger", "account", "category", "income", "expense", "internalTransfer", "adjustment"],
)
async def test_absent_lookup_cannot_cancel_delayed_original_or_allow_duplicate_effects(
    postgres_database_url: str,
    postgres_session: AsyncSession,
    transaction_scope: tuple[User, UUID, UUID, UUID],
    operation: str,
) -> None:
    actor, ledger, account, destination = transaction_scope
    money = {"amount": "12.30", "currency": "CNY"}
    scope = f"/api/finance/ledgers/{ledger}"
    body: dict[str, object]
    if operation == "ledger":
        path, body = "/api/finance/ledgers", {"name": "Delayed Ledger"}
    elif operation == "account":
        path, body = (
            f"{scope}/accounts",
            {
                "name": "Delayed Account",
                "nature": "liability",
                "currency": "CNY",
                "openingBalance": money,
                "trackingStartDate": "2026-10-01",
            },
        )
    elif operation == "category":
        path, body = f"{scope}/categories", {"name": "Delayed Category"}
    elif operation == "adjustment":
        path, body = (
            f"{scope}/balance-adjustments",
            {
                "accountId": str(account),
                "transactionDate": "2026-10-02",
                "expectedDerivedBalance": {"amount": "0", "currency": "CNY"},
                "expectedAccountNature": "asset",
                "targetBalance": money,
            },
        )
    else:
        path, body = (
            f"{scope}/transactions",
            {
                "kind": operation,
                "transactionDate": "2026-10-02",
            },
        )
        body.update(
            {
                "sourceAccountId": str(account),
                "destinationAccountId": str(destination),
                "amount": money,
            }
            if operation == "internalTransfer"
            else {
                "accountId": str(account),
                "economicAmount": money,
                "categoryAllocations": [{"amount": money}],
            }
        )
    headers = {**submission_headers(actor.id), "Content-Type": "application/json"}
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed_body() -> AsyncIterator[bytes]:
        started.set()
        await release.wait()
        yield json.dumps(body).encode()

    async def effects() -> list[int]:
        return [
            (await postgres_session.scalar(select(func.count()).select_from(model))) or 0
            for model in (
                FinanceLedger,
                FinanceAccount,
                FinanceCategory,
                FinanceTransaction,
                FinanceAccountMovement,
                FinanceCategoryAllocation,
                FinanceSubmission,
            )
        ]

    before = await effects()
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        original = asyncio.create_task(client.post(path, content=delayed_body(), headers=headers))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            absent = await client.get(
                f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
            )
            assert absent.status_code == 404
            assert absent.headers["cache-control"] == "no-store"
            assert absent.json()["code"] == "finance_submission_not_found"
            assert await effects() == before
        finally:
            release.set()
        committed = await asyncio.wait_for(original, timeout=5)
        assert committed.status_code == (200 if operation == "adjustment" else 201)
        receipt = committed.json()
        after = await effects()
        expected = {
            "ledger": [1, 0, 0, 0, 0, 0, 1],
            "account": [0, 1, 0, 0, 0, 0, 1],
            "category": [0, 0, 1, 0, 0, 0, 1],
            "income": [0, 0, 0, 1, 1, 1, 1],
            "expense": [0, 0, 0, 1, 1, 1, 1],
            "internalTransfer": [0, 0, 0, 1, 2, 0, 1],
            "adjustment": [0, 0, 0, 1, 1, 0, 1],
        }[operation]
        assert after == [count + delta for count, delta in zip(before, expected, strict=True)]
        lookup = await client.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
        )
        assert lookup.json() == {"state": "terminal", "receipt": receipt}
        replay = await client.post(path, json=body, headers=headers)
        assert replay.status_code == committed.status_code
        assert replay.json() == receipt
        assert await effects() == after
