"""Restore applied Credit at its original receipt through Advance Correction."""

from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient, ReadError, Request, Response
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core_console.modules.finance.advance_correction_models import (
    FinanceAdvanceCorrectionTransaction,
)
from core_console.modules.finance.credit_application_models import (
    FinanceCreditApplicationAllocation,
)
from core_console.modules.finance.queries import get_finance_account_balance
from core_console.modules.users.models import User
from integration.finance_submission_helpers import submission_headers
from integration.test_finance_advance_cash_corrections import share_correction
from integration.test_finance_advance_corrections import correct, money
from integration.test_finance_advance_forgiveness_corrections import _combined, _settle
from integration.test_finance_api import finance_client
from integration.test_finance_credit_applications import application, apply_credit, retain_credit
from integration.test_finance_forgiveness import forgiveness
from integration.test_finance_nested_submission_persistence import _migrate
from integration.test_finance_settlements import advance, advance_client, confirm, repayment

pytestmark = pytest.mark.anyio
__all__ = ["advance_client"]


@pytest.mark.parametrize("newest", ["credit", "cash"])
async def test_forgiveness_then_newest_allocation_across_cash_and_credit(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]], newest: str
) -> None:
    client, actor, base, draft = advance_client
    older = await retain_credit(client, actor, base, draft, "12.00", "2026-10-24")
    newer = await retain_credit(client, actor, base, draft, "15.00", "2026-10-22")
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    ids = [source["obligation"]["id"]]
    unrelated = await advance(client, actor, base, draft, "7.00", "2026-10-21")

    async def cash(date: str) -> dict[str, Any]:
        return await confirm(
            client, actor, base, {**repayment(draft, "60.00"), "transactionDate": date}
        )

    async def credit(date: str) -> dict[str, Any]:
        return await apply_credit(
            client, actor, base, {**application(draft, ids, "20.00"), "transactionDate": date}
        )

    if newest == "credit":
        receipt, applied = await cash("2026-10-28"), await credit("2026-10-26")
    else:
        applied, receipt = await credit("2026-10-28"), await cash("2026-10-26")
    receipt_path = f"{base}/transactions/{receipt['transactionIds'][0]}"
    original_cash = (await client.get(receipt_path)).json()
    forgiven = await _settle(client, actor, base, forgiveness(draft, ids, "20.00"))

    first = await correct(client, actor, base, share_correction(source, "90.00", "10.00"))
    first_obligation = first["after"]["obligations"][0]
    assert first_obligation["cashPaid"] == money("60.00")
    assert first_obligation["creditApplied"] == money("20.00")
    assert first_obligation["forgiven"] == money("10.00")
    assert first_obligation["remaining"] == money("0.00")
    expense = (await client.get(f"{base}/transactions/{forgiven['transactionIds'][0]}")).json()
    assert expense["economicAmount"] == money("10.00")
    assert expense["transactionDate"] == forgiven["transactionDate"]
    assert all(a["restored"] == money("0.00") for a in first["creditApplications"])

    second = await correct(client, actor, base, share_correction(source, "70.00", "30.00"))
    obligation = second["after"]["obligations"][0]
    assert obligation["remaining"] == obligation["forgiven"] == money("0.00")
    assert (await client.get(receipt_path)).json() == original_cash
    cash_detail = (await client.get(f"{base}/settlements/{receipt['id']}")).json()
    assert cash_detail["confirmationOrder"] == receipt["confirmationOrder"]
    # The earlier cash receipt cannot include a Credit application confirmed later,
    # even though that application has an earlier financial date.
    assert cash_detail["allocations"][0]["remainingAfter"] == money(
        "10.00" if newest == "credit" else "0.00"
    )
    forgiveness_detail = (await client.get(f"{base}/settlements/{forgiven['id']}")).json()
    assert forgiveness_detail["confirmationOrder"] == forgiven["confirmationOrder"]
    [forgiveness_allocation] = forgiveness_detail["allocations"]
    assert forgiveness_allocation["remainingAfter"] == forgiven["allocations"][0]["remainingAfter"]
    assert forgiveness_allocation.get(
        "effectiveRemainingAfter", forgiveness_allocation["remainingAfter"]
    ) == money("0.00")
    assert (await client.get(f"{base}/advances/{unrelated['id']}")).json() == unrelated
    [position] = second["participantPositions"]
    assert position["availableCreditBefore"] == money("7.00")
    assert position["availableCreditAfter"] == money("17.00")
    assert position["outstandingReceivablesAfter"] == money("7.00")
    detail = (await client.get(f"{base}/settlements/{applied['id']}")).json()
    assert {**detail, "allocations": applied["allocations"]} == applied
    for original, effective in zip(applied["allocations"], detail["allocations"], strict=True):
        assert original == {k: v for k, v in effective.items() if k != "effectiveCreditApplied"}
    if newest == "credit":
        assert obligation["cashPaid"] == money("60.00")
        assert obligation["creditApplied"] == money("10.00")
        assert second.get("cashReleases", []) == []
        assert [
            (a["position"], a["origin"]["settlementId"], a["restored"])
            for a in second["creditApplications"]
        ] == [(2, newer["id"], money("8.00")), (1, older["id"], money("2.00"))]
        assert [a["effectiveCreditApplied"] for a in detail["allocations"]] == [
            money("10.00"),
            money("0.00"),
        ]
        reapplied = await apply_credit(
            client, actor, base, application(draft, [unrelated["obligation"]["id"]], "3.00")
        )
        assert [
            (a["originSettlementId"], a["creditApplied"]) for a in reapplied["allocations"]
        ] == [(older["id"], money("2.00")), (newer["id"], money("1.00"))]
    else:
        assert obligation["cashPaid"] == money("50.00")
        assert obligation["creditApplied"] == money("20.00")
        [release] = second["cashReleases"]
        assert release["settlementId"] == receipt["id"]
        assert release["released"] == money("10.00")
        assert detail == applied
    assert (await client.get(f"{base}/advances/{source['id']}/corrections")).json() == [
        first,
        second,
    ]


async def test_combined_remaining_uses_effective_credit_at_original_confirmation(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    await retain_credit(client, actor, base, draft, "20.00")
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    await apply_credit(
        client, actor, base, application(draft, [source["obligation"]["id"]], "20.00")
    )
    combined = await _settle(
        client, actor, base, {**_combined(draft, source), "forgivenessAmount": money("20.00")}
    )
    correction = await correct(client, actor, base, share_correction(source, "10.00", "90.00"))
    [restoration] = correction["creditApplications"]
    assert restoration["restored"] == restoration["appliedAfter"] == money("10.00")
    detail = (await client.get(f"{base}/settlements/{combined['id']}")).json()
    assert detail["confirmationOrder"] == combined["confirmationOrder"]
    cash, forgiven = detail["allocations"]
    assert cash["cashPaid"] == money("60.00")
    assert cash["effectiveCashPaid"] == cash["effectiveRemainingAfter"] == money("0.00")
    assert cash["remainingAfter"] == money("20.00")
    assert forgiven["forgiven"] == money("20.00")
    assert forgiven["effectiveForgiven"] == forgiven["remainingAfter"] == money("0.00")


@pytest.mark.parametrize("phase", ["rollback", "response_loss"])
async def test_restoration_recovery_observes_committed_effects_without_duplicate_transfers(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    client, actor, base, draft = advance_client
    receipt = await retain_credit(client, actor, base, draft, "20.00")
    source = await advance(client, actor, base, draft, "25.00", "2026-10-20")
    allocated = await apply_credit(
        client, actor, base, application(draft, [source["obligation"]["id"]], "20.00")
    )
    body = share_correction(source, "10.00", "15.00")
    path = f"{base}/advance-corrections"
    preview = await client.post(f"{path}/preview", json=body)
    assert preview.status_code == 200, preview.text
    [application_plan] = preview.json()["creditApplications"]
    assert application_plan["restored"] == money("10.00")
    command = {**body, "previewToken": preview.json()["previewToken"]}
    headers = submission_headers(actor.id)
    allocation_id = (UUID(allocated["id"]), 1)
    liability_id = UUID(receipt["creditOrigin"]["liabilityAccountId"])
    ledger_id = UUID(receipt["ledgerId"])
    receivable_id = UUID(source["obligation"]["receivableAccountId"])
    real_flush = AsyncSession.flush
    real_handle = ASGITransport.handle_async_request

    async def assert_effects(committed: bool) -> None:
        allocation = await postgres_session.get(FinanceCreditApplicationAllocation, allocation_id)
        assert allocation is not None
        assert allocation.amount == Decimal("20.00")
        assert allocation.effective_amount == (Decimal("10.00") if committed else None)
        for account_id, expected in [
            (liability_id, "10.00" if committed else "0.00"),
            (receivable_id, "0.00" if committed else "5.00"),
        ]:
            balance = await get_finance_account_balance(
                postgres_session, ledger_id=ledger_id, account_id=account_id
            )
            assert balance is not None and balance.current_balance == Decimal(expected)
        assert await postgres_session.scalar(
            select(func.count()).select_from(FinanceAdvanceCorrectionTransaction)
        ) == int(committed)
        await postgres_session.rollback()

    async def fail_after_transfer(session: AsyncSession, objects: Any = None) -> None:
        restoring = any(isinstance(row, FinanceAdvanceCorrectionTransaction) for row in session.new)
        await real_flush(session, objects)
        if restoring:
            raise RuntimeError("injected failure after writing restoration")

    async def lose_response(transport: ASGITransport, request: Request) -> Response:
        result = await real_handle(transport, request)
        assert result.status_code == 201, result.text
        await assert_effects(committed=True)
        raise ReadError("committed correction response lost", request=request)

    with monkeypatch.context() as fault:
        if phase == "rollback":
            fault.setattr(AsyncSession, "flush", fail_after_transfer)
            result = await client.post(path, json=command, headers=headers)
            assert result.status_code == 500, result.text
            await assert_effects(committed=False)
        else:
            fault.setattr(ASGITransport, "handle_async_request", lose_response)
            with pytest.raises(ReadError):
                await client.post(path, json=command, headers=headers)

    await postgres_session.refresh(actor)
    async with finance_client(database_url=postgres_database_url, actor=actor) as restarted:
        lookup = await restarted.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
        )
        assert lookup.json()["state"] == ("unfinished" if phase == "rollback" else "terminal")
        result = await restarted.post(path, json=command, headers=headers)
        assert result.status_code == 201, result.text
        assert (await restarted.post(path, json=command, headers=headers)).json() == result.json()
        history = (await restarted.get(f"{base}/advances/{source['id']}/corrections")).json()
        assert len(history) == 1
        assert history[0]["creditApplications"] == [application_plan]
    await assert_effects(committed=True)


async def test_restored_position_and_transactions_reject_manual_changes(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_engine: AsyncEngine,
) -> None:
    client, actor, base, draft = advance_client
    origin = await retain_credit(client, actor, base, draft, "20.00")
    source = await advance(client, actor, base, draft, "20.00", "2026-10-20")
    applied = await apply_credit(
        client, actor, base, application(draft, [source["obligation"]["id"]], "20.00")
    )
    result = await correct(client, actor, base, share_correction(source, "10.00", "10.00"))
    [restoration] = result["creditApplications"]
    liability = origin["creditOrigin"]["liabilityAccountId"]
    entry = {
        "kind": "income",
        "accountId": liability,
        "transactionDate": "2026-10-29",
        "economicAmount": money("1.00"),
        "categoryAllocations": [{"amount": money("1.00"), "categoryId": None}],
    }
    for id in [
        origin["creditOrigin"]["transactionId"],
        applied["transactionIds"][0],
        restoration["financialEffect"]["transactionId"],
    ]:
        path = f"{base}/transactions/{id}"
        for response in (await client.delete(path), await client.put(path, json=entry)):
            assert response.status_code == 409, response.text
            assert response.json()["code"] == "finance_linked_record_protected"
    for response in (
        await client.patch(f"{base}/accounts/{liability}", json={"openingBalance": money("1.00")}),
        await client.post(f"{base}/transactions", json=entry, headers=submission_headers(actor.id)),
        await client.post(
            f"{base}/balance-adjustments",
            json={
                "accountId": liability,
                "transactionDate": "2026-10-29",
                "expectedDerivedBalance": money("10.00"),
                "expectedAccountNature": "liability",
                "targetBalance": money("0.00"),
            },
            headers=submission_headers(actor.id),
        ),
    ):
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "finance_linked_record_protected"
    [credit] = (await client.get(f"{base}/participants/{draft['participantId']}/credits")).json()
    assert credit["settlementId"] == origin["id"]
    assert credit["available"] == money("10.00")
    with pytest.raises(DBAPIError, match="Advance Corrections have restored applied Credit"):
        await _migrate(postgres_engine, revision="20261122_01", downgrade=True)
    history = await client.get(f"{base}/advances/{source['id']}/corrections")
    assert history.json() == [result]
