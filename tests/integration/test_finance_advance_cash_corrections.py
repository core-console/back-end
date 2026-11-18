"""Advance Corrections preserve confirmed cash and its receipt-origin credit."""

from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core_console.modules.finance.settlement_models import FinanceSettlementAllocation
from core_console.modules.users.models import User
from integration.finance_submission_helpers import submission_headers
from integration.test_finance_advance_corrections import correct, correction, money
from integration.test_finance_credit_applications import application, apply_credit
from integration.test_finance_nested_submission_persistence import _migrate
from integration.test_finance_settlement_corrections import (
    correct as correct_receipt,
)
from integration.test_finance_settlement_corrections import (
    correction as receipt_correction,
)
from integration.test_finance_settlements import advance, advance_client, confirm, repayment
from integration.test_finance_surplus import accepted, confirm_income

pytestmark = pytest.mark.anyio
__all__ = ["advance_client"]


def share_correction(source: dict[str, Any], principal: str, owner: str) -> dict[str, Any]:
    return {
        **correction(source, amount=source["amount"]["amount"], share=principal),
        "ownerShare": money(owner),
    }


async def test_unpaid_remainder_then_cash_release_preserves_receipt_and_financial_positions(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_engine: AsyncEngine,
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "200.00", "2026-10-20")
    receipt = await confirm(client, actor, base, repayment(draft, "80.00"))
    receipt_url = f"{base}/settlements/{receipt['id']}"
    assert receipt["allocations"][0]["remainingAfter"] == money("120.00")
    first = await correct(client, actor, base, share_correction(source, "180.00", "20.00"))
    assert first.get("cashReleases", []) == []
    assert first["after"]["obligations"][0]["cashPaid"] == money("80.00")
    assert first["after"]["obligations"][0]["remaining"] == money("100.00")
    after_first = (await client.get(receipt_url)).json()

    second = await correct(client, actor, base, share_correction(source, "70.00", "130.00"))
    assert second["before"] == first["after"]
    obligation = second["after"]["obligations"][0]
    assert obligation["cashPaid"] == money("70.00")
    assert obligation["remaining"] == money("0.00")
    release = second["cashReleases"][0]
    assert release["settlementId"] == receipt["id"]
    assert release["receiptAmount"] == money("80.00")
    assert release["cashBefore"] == money("80.00")
    assert release["cashAfter"] == money("70.00")
    assert release["released"] == release["creditAfter"] == money("10.00")
    current_receipt = (await client.get(receipt_url)).json()
    assert current_receipt["allocations"][0] == {
        **receipt["allocations"][0],
        "cashPaid": money("70.00"),
        "remainingAfter": money("0.00"),
    }
    assert after_first["allocations"][0] == {
        **receipt["allocations"][0],
        "remainingAfter": money("100.00"),
    }
    assert current_receipt["amount"] == receipt["amount"] == money("80.00")
    assert current_receipt["confirmationOrder"] == receipt["confirmationOrder"]
    async with AsyncSession(postgres_engine) as session:
        allocation = await session.get(
            FinanceSettlementAllocation,
            (UUID(receipt["id"]), receipt["allocations"][0]["position"]),
        )
        assert allocation is not None
        assert allocation.cash_paid == Decimal("80.00")
        assert allocation.remaining_after == Decimal("120.00")
        assert str(allocation.transaction_id) == receipt["transactionIds"][0]
    assert current_receipt["creditOrigin"]["amount"] == release["released"]
    assert (
        current_receipt["creditOrigin"]["transactionId"]
        == release["financialEffect"]["transactionId"]
    )
    accounts = {a["id"]: a for a in (await client.get(f"{base}/accounts")).json()}
    assert accounts[str(draft["fundingAccountId"])]["currentBalance"] == money("-20.00")
    assert accounts[obligation["receivableAccountId"]]["currentBalance"] == money("0.00")
    assert accounts[release["creditAccountId"]]["currentBalance"] == money("10.00")
    positions = (await client.get(f"{base}/participants/{draft['participantId']}/positions")).json()
    assert positions[0]["outstandingReceivables"] == money("0.00")
    assert positions[0]["availableCredits"] == money("10.00")
    history = (await client.get(f"{base}/advances/{source['id']}/corrections")).json()
    assert history == [first, second]
    month = (await client.get(f"{base}/overview", params={"month": "2026-10"})).json()
    summary = month["monthSummaryByCurrency"][0]
    assert summary["expense"] == money("130.00")
    assert summary["income"] == money("0.00")


async def test_income_receipt_keeps_income_and_links_released_credit(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    receipt = await confirm_income(client, actor, base, accepted(draft))
    original_transactions = {
        tx: (await client.get(f"{base}/transactions/{tx}")).json()
        for tx in receipt["transactionIds"]
    }
    body = share_correction(source, "70.00", "30.00")
    response = await client.post(f"{base}/advance-corrections/preview", json=body)
    assert response.status_code == 200, response.text
    preview = response.json()
    correction = await correct(client, actor, base, body)
    assert correction["after"] == preview["after"]
    assert correction["cashReleases"] == preview["cashReleases"]
    release = correction["cashReleases"][0]
    credit_tx = release["financialEffect"]["transactionId"]

    detail = (await client.get(f"{base}/settlements/{receipt['id']}")).json()
    for key in ("id", "amount", "confirmedAt", "confirmationOrder", "incomeTransactionId"):
        assert detail[key] == receipt[key]
    assert detail["allocations"][0]["cashPaid"] == money("70.00")
    assert detail["acceptedSurplus"] == receipt["acceptedSurplus"] == money("2.00")
    assert set(detail["transactionIds"]) == {*receipt["transactionIds"], credit_tx}
    for tx, original in original_transactions.items():
        assert (await client.get(f"{base}/transactions/{tx}")).json() == original

    participant = f"{base}/participants/{draft['participantId']}"
    origin = detail["creditOrigin"]
    assert (await client.get(f"{participant}/credits")).json() == [origin]
    assert origin["settlementId"] == release["settlementId"] == receipt["id"]
    assert origin["amount"] == origin["available"] == release["released"] == money("30.00")
    assert origin["liabilityAccountId"] == release["creditAccountId"]
    assert origin["transactionId"] == credit_tx
    transfer = await client.get(f"{base}/transactions/{credit_tx}")
    assert transfer.status_code == 200, transfer.text
    assert transfer.json()["kind"] == "internalTransfer"
    assert Decimal(detail["amount"]["amount"]) == sum(
        Decimal(part["amount"])
        for part in (
            detail["allocations"][0]["cashPaid"],
            detail["acceptedSurplus"],
            origin["amount"],
        )
    )
    positions = (await client.get(f"{participant}/positions")).json()
    assert positions[0]["outstandingReceivables"] == money("0.00")
    assert positions[0]["availableCredits"] == origin["available"]
    assert (await client.get(f"{participant}/settlements")).json() == [detail]


async def test_newest_confirmation_releases_first_and_increasing_principal_keeps_credit(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "90.00", "2026-10-20")
    first = await confirm(client, actor, base, repayment(draft, "50.00"))
    second = await confirm(
        client, actor, base, {**repayment(draft, "40.00"), "transactionDate": "2026-10-21"}
    )
    corrected = await correct(client, actor, base, share_correction(source, "60.00", "30.00"))
    release = corrected["cashReleases"][0]
    assert release["settlementId"] == second["id"]
    assert release["confirmationOrder"] == second["confirmationOrder"]
    assert release["released"] == money("30.00")
    first_after = (await client.get(f"{base}/settlements/{first['id']}")).json()
    second_after = (await client.get(f"{base}/settlements/{second['id']}")).json()
    assert first_after["allocations"][0]["cashPaid"] == money("50.00")
    assert first_after["allocations"][0]["remainingAfter"] == money("10.00")
    assert second_after["allocations"][0]["cashPaid"] == money("10.00")
    assert second_after["allocations"][0]["remainingAfter"] == money("0.00")
    increased = await correct(client, actor, base, share_correction(source, "80.00", "10.00"))
    assert increased.get("cashReleases", []) == []
    assert increased["after"]["obligations"][0]["remaining"] == money("20.00")
    positions = (await client.get(f"{base}/participants/{draft['participantId']}/positions")).json()
    assert positions[0]["availableCredits"] == money("30.00")


async def test_receipt_correction_after_release_does_not_implicitly_apply_credit(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "90.00", "2026-10-20")
    receipt = await confirm(client, actor, base, repayment(draft, "90.00"))
    await correct(client, actor, base, share_correction(source, "60.00", "30.00"))
    await correct(client, actor, base, share_correction(source, "80.00", "10.00"))
    result = await correct_receipt(
        client, actor, base, receipt_correction(receipt, "100.00", "retainCredit")
    )
    assert result["before"]["retainedCredit"] == money("30.00")
    assert result["after"]["retainedCredit"] == money("30.00")
    assert result["after"]["allocations"][0]["cashPaid"] == money("70.00")
    effective = (await client.get(f"{base}/advances/{source['id']}")).json()
    assert effective["obligations"][0]["remaining"] == money("10.00")
    positions = (await client.get(f"{base}/participants/{draft['participantId']}/positions")).json()
    assert positions[0]["availableCredits"] == money("30.00")


async def test_cash_arriving_after_preview_requires_fresh_confirmation(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    first_receipt = await confirm(client, actor, base, repayment(draft, "50.00"))
    body = share_correction(source, "40.00", "60.00")
    first = (await client.post(f"{base}/advance-corrections/preview", json=body)).json()
    second_receipt = await confirm(client, actor, base, repayment(draft, "20.00"))
    result = await client.post(
        f"{base}/advance-corrections",
        json={**body, "previewToken": first["previewToken"]},
        headers=submission_headers(actor.id),
    )
    assert result.status_code == 409, result.text
    assert result.json()["code"] == "finance_advance_correction_preview_stale"
    assert (await client.get(f"{base}/advances/{source['id']}/corrections")).json() == []
    current = await correct(client, actor, base, body)
    assert [(r["settlementId"], r["released"]) for r in current["cashReleases"]] == [
        (second_receipt["id"], money("20.00")),
        (first_receipt["id"], money("10.00")),
    ]


async def test_applied_credit_origin_requires_later_lifecycle_correction(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "90.00", "2026-10-20")
    await confirm(client, actor, base, repayment(draft, "90.00"))
    await correct(client, actor, base, share_correction(source, "60.00", "30.00"))
    other = await advance(client, actor, base, draft, "20.00", "2026-10-20")
    await apply_credit(
        client, actor, base, application(draft, [other["obligation"]["id"]], "10.00")
    )
    rejected = await client.post(
        f"{base}/advance-corrections/preview", json=share_correction(source, "50.00", "40.00")
    )
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "finance_advance_correction_ineligible"


async def test_repeated_releases_keep_one_origin_and_protect_each_restatement(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "90.00", "2026-10-20")
    receipt = await confirm(client, actor, base, repayment(draft, "90.00"))
    first = await correct(client, actor, base, share_correction(source, "80.00", "10.00"))
    second = await correct(client, actor, base, share_correction(source, "60.00", "30.00"))
    initial, later = first["cashReleases"][0], second["cashReleases"][0]
    assert later["settlementId"] == initial["settlementId"] == receipt["id"]
    assert later["creditAccountId"] == initial["creditAccountId"]
    assert later["creditBefore"] == money("10.00")
    assert later["creditAfter"] == money("30.00")
    detail = (await client.get(f"{base}/settlements/{receipt['id']}")).json()
    assert detail["transactionIds"] == [
        *receipt["transactionIds"],
        initial["financialEffect"]["transactionId"],
        later["financialEffect"]["transactionId"],
    ]
    for release in (initial, later):
        response = await client.delete(
            f"{base}/transactions/{release['financialEffect']['transactionId']}"
        )
        assert response.status_code == 409
        assert response.json()["code"] == "finance_linked_record_protected"
    blocked = await client.patch(
        f"{base}/accounts/{later['creditAccountId']}", json={"openingBalance": money("0.00")}
    )
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "finance_linked_record_protected"


async def test_released_credit_is_consumed_oldest_receipt_first(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "90.00", "2026-10-20")
    first = await confirm(client, actor, base, repayment(draft, "50.00"))
    second = await confirm(client, actor, base, repayment(draft, "40.00"))
    result = await correct(client, actor, base, share_correction(source, "10.00", "80.00"))
    assert [r["settlementId"] for r in result["cashReleases"]] == [second["id"], first["id"]]
    other = await advance(client, actor, base, draft, "60.00", "2026-10-20")
    body = application(draft, [other["obligation"]["id"]], "50.00")
    preview = await client.post(f"{base}/settlements/preview", json=body)
    assert preview.status_code == 200, preview.text
    origins = preview.json()["origins"]
    assert [o["originSettlementId"] for o in origins] == [first["id"], second["id"]]
    assert [o["amount"] for o in origins] == [money("40.00"), money("10.00")]


async def test_downgrade_refuses_to_discard_a_retained_cash_correction(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_engine: AsyncEngine,
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "90.00", "2026-10-20")
    await confirm(client, actor, base, repayment(draft, "90.00"))
    result = await correct(client, actor, base, share_correction(source, "60.00", "30.00"))
    with pytest.raises(DBAPIError, match="Advance correction evidence"):
        await _migrate(postgres_engine, revision="20261102_01", downgrade=True)
    assert (await client.get(f"{base}/advances/{source['id']}/corrections")).json() == [result]
