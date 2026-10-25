"""Combined resolution preserves ownership, exact Money and linked protections."""

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.users.models import User
from integration.finance_submission_helpers import create_finance_resource, submission_headers
from integration.test_finance_api import finance_client
from integration.test_finance_combined_settlements import combined
from integration.test_finance_settlements import advance, advance_client

pytestmark = pytest.mark.anyio
__all__ = ["advance_client"]


async def test_combined_cash_and_expense_cannot_be_changed_through_manual_finance(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    receivable = source["obligation"]["receivableAccountId"]
    body = combined(draft, [source["obligation"]["id"]], "70.00", "20.00")
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    created = await client.post(
        f"{base}/settlements",
        json={**body, "previewToken": preview["previewToken"]},
        headers={**submission_headers(actor.id), "Finance-Command-Version": "6"},
    )
    assert created.status_code == 201, created.text
    detail_url = f"{base}/settlements/{created.json()['outcome']['resource']['id']}"
    retained = (await client.get(detail_url)).json()
    money = {"amount": "1.00", "currency": "CNY"}
    expense = {
        "kind": "expense",
        "accountId": receivable,
        "transactionDate": "2026-10-22",
        "economicAmount": money,
        "categoryAllocations": [{"amount": money}],
    }
    transfer = {
        "kind": "internalTransfer",
        "sourceAccountId": receivable,
        "destinationAccountId": draft["fundingAccountId"],
        "amount": money,
        "transactionDate": "2026-10-22",
    }
    adjustment = {
        "accountId": receivable,
        "transactionDate": "2026-10-22",
        "expectedAccountNature": "asset",
        "expectedDerivedBalance": {"amount": "10.00", "currency": "CNY"},
        "targetBalance": money,
    }
    for path, command in [
        ("transactions", expense),
        ("transactions", {**expense, "kind": "income"}),
        ("transactions", transfer),
        ("balance-adjustments", adjustment),
    ]:
        rejected = await client.post(
            f"{base}/{path}", json=command, headers=submission_headers(actor.id)
        )
        assert (
            rejected.status_code == 409
            and rejected.json()["code"] == "finance_linked_record_protected"
        ), rejected.text
    for transaction_id in retained["transactionIds"]:
        transaction = (await client.get(f"{base}/transactions/{transaction_id}")).json()
        command = expense if transaction["kind"] == "expense" else transfer
        for method in ("PUT", "DELETE"):
            rejected = await client.request(
                method,
                f"{base}/transactions/{transaction_id}",
                json=command if method == "PUT" else None,
            )
            assert (
                rejected.status_code == 409
                and rejected.json()["code"] == "finance_linked_record_protected"
            ), rejected.text
    for method, path, mutation in [
        ("PATCH", f"accounts/{receivable}", {"openingBalance": money}),
        ("PATCH", f"accounts/{receivable}", {"trackingStartDate": "2026-10-02"}),
        ("POST", f"accounts/{receivable}/archive", None),
    ]:
        rejected = await client.request(method, f"{base}/{path}", json=mutation)
        assert (
            rejected.status_code == 409
            and rejected.json()["code"] == "finance_linked_record_protected"
        ), rejected.text
    managed_receiving = await client.post(
        f"{base}/settlements/preview", json={**body, "receivingAccountId": receivable}
    )
    assert (
        managed_receiving.status_code == 409
        and managed_receiving.json()["code"] == "finance_linked_record_protected"
    )
    assert (await client.delete(detail_url)).status_code == 405
    assert (await client.get(detail_url)).json() == retained
    obligations = (
        await client.get(f"{base}/participants/{draft['participantId']}/obligations")
    ).json()
    assert obligations[0]["remaining"]["amount"] == "10.00"


async def test_combined_recovery_identity_and_owner_ledger_scopes_are_isolated(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    postgres_database_url: str,
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = combined(draft, [source["obligation"]["id"]])
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    command = {**body, "previewToken": preview["previewToken"]}
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "6"}
    created = await client.post(f"{base}/settlements", json=command, headers=headers)
    assert created.status_code == 201, created.text
    conflict = await client.post(
        f"{base}/settlements", json={**command, "reason": "changed"}, headers=headers
    )
    assert (
        conflict.status_code == 409
        and conflict.json()["code"] == "finance_submission_content_conflict"
    )
    version_conflict = await client.post(
        f"{base}/settlements", json=command, headers={**headers, "Finance-Command-Version": "5"}
    )
    assert version_conflict.status_code == 409
    url = f"{base}/settlements/{created.json()['outcome']['resource']['id']}"
    ledger = await client.post(
        "/api/finance/ledgers", json={"name": "Other"}, headers=submission_headers(actor.id)
    )
    other_base = f"/api/finance/ledgers/{ledger.json()['outcome']['resource']['id']}"
    assert (
        await client.get(f"{other_base}/settlements/{created.json()['outcome']['resource']['id']}")
    ).status_code == 404
    assert (await client.post(f"{other_base}/settlements/preview", json=body)).status_code == 404
    other = User(
        identity_issuer="https://identity.example.test/combined",
        identity_subject="other",
        status="active",
    )
    postgres_session.add(other)
    await postgres_session.commit()
    async with finance_client(database_url=postgres_database_url, actor=other) as foreign:
        foreign_headers = {**submission_headers(other.id), "Finance-Command-Version": "6"}
        assert (await foreign.get(url)).status_code == 404
        assert (await foreign.post(f"{base}/settlements/preview", json=body)).status_code == 404
        assert (
            await foreign.post(f"{base}/settlements", json=command, headers=foreign_headers)
        ).status_code == 404
        assert (
            await foreign.get(
                f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=foreign_headers
            )
        ).status_code == 404
    lookup = await client.get(
        f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
    )
    assert lookup.json()["receipt"] == created.json()
    assert (
        await client.post(f"{base}/settlements", json=command, headers=headers)
    ).json() == created.json()


@pytest.mark.parametrize(
    "currency,principal,cash,forgiven,remainder,liability",
    [
        ("JPY", "100", "98", "2", "0", "2"),
        ("USD", "100.00", "98.00", "1.00", "1.00", "2.00"),
        (
            "CNY",
            "123456789012345678901234567890.02",
            "123456789012345678901234567890.00",
            "0.01",
            "0.01",
            "0.02",
        ),
    ],
)
async def test_combined_exact_money_and_future_events_affect_positions_immediately(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    currency: str,
    principal: str,
    cash: str,
    forgiven: str,
    remainder: str,
    liability: str,
) -> None:
    client, actor, base, draft = advance_client
    zero = "0" if currency == "JPY" else "0.00"
    account = await create_finance_resource(
        client,
        f"{base}/accounts",
        owner_id=actor.id,
        json={
            "name": currency,
            "nature": "liability",
            "currency": currency,
            "openingBalance": {"amount": zero, "currency": currency},
            "trackingStartDate": "2026-10-01",
        },
    )
    account_id = account.json()["id"]
    source_body: dict[str, Any] = {
        "fundingAccountId": account_id,
        "transactionDate": "2026-11-01",
        "amount": {"amount": principal, "currency": currency},
        "ownerShare": {"amount": zero, "currency": currency},
        "participantShares": [
            {
                "participantId": draft["participantId"],
                "amount": {"amount": principal, "currency": currency},
            }
        ],
    }
    preview = (await client.post(f"{base}/advances/preview", json=source_body)).json()
    result = await client.post(
        f"{base}/advances",
        json={**source_body, "previewToken": preview["previewToken"]},
        headers={**submission_headers(actor.id), "Finance-Command-Version": "2"},
    )
    assert result.status_code == 201, result.text
    obligation = (
        await client.get(f"{base}/advances/{result.json()['outcome']['resource']['id']}")
    ).json()["obligations"][0]
    body = {
        **combined(draft, [obligation["id"]]),
        "receivingAccountId": account_id,
        "transactionDate": "2026-11-02",
        "amount": {"amount": cash, "currency": currency},
        "forgivenessAmount": {"amount": forgiven, "currency": currency},
    }
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    assert preview["outstandingAfter"]["amount"] == remainder
    assert preview["receivingBalanceAfter"]["amount"] == liability
    result = await client.post(
        f"{base}/settlements",
        json={**body, "previewToken": preview["previewToken"]},
        headers={**submission_headers(actor.id), "Finance-Command-Version": "6"},
    )
    assert result.status_code == 201, result.text
    accounts = {a["id"]: a for a in (await client.get(f"{base}/accounts")).json()}
    assert accounts[account_id]["currentBalance"]["amount"] == liability
    assert accounts[obligation["receivableAccountId"]]["currentBalance"]["amount"] == remainder
    summary = (await client.get(f"{base}/overview", params={"month": "2026-11"})).json()[
        "monthSummaryByCurrency"
    ]
    assert next(s for s in summary if s["currency"] == currency)["expense"]["amount"] == forgiven
    obligation = (
        await client.get(f"{base}/participants/{draft['participantId']}/obligations")
    ).json()[0]
    assert (
        obligation["cashPaid"]["amount"],
        obligation["forgiven"]["amount"],
        obligation["remaining"]["amount"],
    ) == (cash, forgiven, remainder)
