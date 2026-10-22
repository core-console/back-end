"""Credit positions remain attributable and protected at ordinary Finance boundaries."""

from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.users.models import User
from integration.finance_submission_helpers import create_finance_resource, submission_headers
from integration.test_finance_advances import advance_client as advance_client
from integration.test_finance_api import finance_client
from integration.test_finance_credits import confirm_credit, retained

pytestmark = pytest.mark.anyio


async def test_credit_liability_and_linked_receipt_are_protected_from_manual_mutations(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    detail = await confirm_credit(client, actor, base, retained(draft, "50.00"))
    origin = detail["creditOrigin"]
    account_id = origin["liabilityAccountId"]
    money = {"amount": "1.00", "currency": "CNY"}
    transfer = {
        "kind": "internalTransfer",
        "sourceAccountId": account_id,
        "destinationAccountId": draft["fundingAccountId"],
        "transactionDate": "2026-10-22",
        "amount": money,
    }
    for kind in ("income", "expense", "internalTransfer", "adjustment", "advance", "settlement"):
        body: dict[str, object]
        if kind in ("income", "expense"):
            path, body, version = (
                "transactions",
                {
                    "kind": kind,
                    "accountId": account_id,
                    "transactionDate": "2026-10-22",
                    "economicAmount": money,
                    "categoryAllocations": [{"amount": money}],
                },
                "1",
            )
        elif kind == "internalTransfer":
            path, body, version = "transactions", transfer, "1"
        elif kind == "adjustment":
            path, body, version = (
                "balance-adjustments",
                {
                    "accountId": account_id,
                    "transactionDate": "2026-10-22",
                    "expectedAccountNature": "liability",
                    "expectedDerivedBalance": {"amount": "50.00", "currency": "CNY"},
                    "targetBalance": money,
                },
                "1",
            )
        elif kind == "advance":
            path, body, version = "advances/preview", {**draft, "fundingAccountId": account_id}, "1"
        else:
            path, body, version = (
                "settlements/preview",
                {**retained(draft), "receivingAccountId": account_id},
                "2",
            )
        blocked = await client.post(
            f"{base}/{path}",
            json=body,
            headers={**submission_headers(actor.id), "Finance-Command-Version": version},
        )
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["code"] == "finance_linked_record_protected"
    for method, path, mutation in [
        ("PATCH", f"accounts/{account_id}", {"openingBalance": money}),
        ("PATCH", f"accounts/{account_id}", {"trackingStartDate": "2026-10-01"}),
        ("POST", f"accounts/{account_id}/archive", None),
        ("POST", f"accounts/{account_id}/unarchive", None),
        ("PUT", f"transactions/{origin['transactionId']}", transfer),
        ("DELETE", f"transactions/{origin['transactionId']}", None),
    ]:
        response = await client.request(method, f"{base}/{path}", json=mutation)
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "finance_linked_record_protected"
    assert (await client.get(f"{base}/settlements/{detail['id']}")).json() == detail
    assert (await client.get(f"{base}/participants/{draft['participantId']}/positions")).json()[0][
        "availableCredits"
    ]["amount"] == "50.00"


@pytest.mark.parametrize("currency,amount", [("USD", "50.00"), ("JPY", "50")])
async def test_liability_receipt_and_multiple_currency_positions_never_create_income(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]], currency: str, amount: str
) -> None:
    client, actor, base, draft = advance_client
    await confirm_credit(client, actor, base, retained(draft, "2.00"))
    account = await create_finance_resource(
        client,
        f"{base}/accounts",
        owner_id=actor.id,
        json={
            "name": "Card",
            "nature": "liability",
            "currency": currency,
            "openingBalance": {
                "amount": "0" if currency == "JPY" else "0.00",
                "currency": currency,
            },
            "trackingStartDate": "2026-10-01",
        },
    )
    body = {
        **retained(draft),
        "receivingAccountId": account.json()["id"],
        "amount": {"amount": amount, "currency": currency},
    }
    detail = await confirm_credit(client, actor, base, body)
    assert detail["creditOrigin"]["available"] == {"amount": amount, "currency": currency}
    accounts = {a["id"]: a for a in (await client.get(f"{base}/accounts")).json()}
    assert accounts[account.json()["id"]]["currentBalance"]["amount"] == f"-{amount}"
    assert (
        accounts[detail["creditOrigin"]["liabilityAccountId"]]["currentBalance"]["amount"] == amount
    )
    positions = (await client.get(f"{base}/participants/{draft['participantId']}/positions")).json()
    assert [p["currency"] for p in positions] == sorted(["CNY", currency])
    assert [p["availableCredits"]["amount"] for p in positions] == ["2.00", amount]
    overview = (await client.get(f"{base}/overview", params={"month": "2026-10"})).json()
    assert all(p["income"]["amount"] in ("0.00", "0") for p in overview["monthSummaryByCurrency"])


async def test_credit_reads_and_receiving_account_respect_ledger_and_owner_boundaries(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    postgres_database_url: str,
) -> None:
    client, actor, base, draft = advance_client
    detail = await confirm_credit(client, actor, base, retained(draft, "50.00"))
    other_ledger = await client.post(
        "/api/finance/ledgers", json={"name": "Other"}, headers=submission_headers(actor.id)
    )
    other_base = f"/api/finance/ledgers/{other_ledger.json()['outcome']['resource']['id']}"
    for suffix in ("credits", "positions"):
        assert (
            await client.get(f"{other_base}/participants/{draft['participantId']}/{suffix}")
        ).status_code == 404
        assert (await client.get(f"{base}/participants/{uuid4()}/{suffix}")).status_code == 404
    assert (
        await client.post(f"{other_base}/settlements/preview", json=retained(draft))
    ).status_code == 404
    other = User(
        identity_issuer="https://identity.example.test/credit",
        identity_subject="other",
        status="active",
    )
    postgres_session.add(other)
    await postgres_session.commit()
    async with finance_client(database_url=postgres_database_url, actor=other) as stranger:
        for suffix in ("credits", "positions"):
            assert (
                await stranger.get(f"{base}/participants/{draft['participantId']}/{suffix}")
            ).status_code == 404
        assert (await stranger.get(f"{base}/settlements/{detail['id']}")).status_code == 404
