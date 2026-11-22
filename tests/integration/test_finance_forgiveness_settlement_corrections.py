"""Restate forgiveness and combined Settlements through guarded PostgreSQL."""

from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient, ReadError, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from core_console.modules.finance.models import FinanceCategoryAllocation, FinanceTransaction
from core_console.modules.finance.settlement_correction_models import FinanceSettlementCorrection
from core_console.modules.finance.settlement_models import FinanceSettlement
from core_console.modules.users.models import User
from integration.finance_submission_helpers import submission_headers
from integration.test_finance_advance_corrections import correct as correct_advance
from integration.test_finance_advance_corrections import correction as advance_correction
from integration.test_finance_combined_settlements import combined
from integration.test_finance_credit_applications import retain_credit
from integration.test_finance_credit_returns import credit_return
from integration.test_finance_forgiveness import forgiveness
from integration.test_finance_settlement_corrections import money
from integration.test_finance_settlements import advance, advance_client, confirm, repayment

pytestmark = pytest.mark.anyio
__all__ = ["advance_client"]


async def settle(
    client: AsyncClient, actor: User, base: str, body: dict[str, Any]
) -> dict[str, Any]:
    preview = await client.post(f"{base}/settlements/preview", json=body)
    assert preview.status_code == 200, preview.text
    result = await client.post(
        f"{base}/settlements",
        json={**body, "previewToken": preview.json()["previewToken"]},
        headers={
            **submission_headers(actor.id),
            "Finance-Command-Version": "6" if body["kind"] == "combined" else "5",
        },
    )
    assert result.status_code == 201, result.text
    detail = await client.get(f"{base}/settlements/{result.json()['outcome']['resource']['id']}")
    assert detail.status_code == 200, detail.text
    return dict(detail.json())


def intent(
    source: dict[str, Any], cash: str, forgiven: str, excess: str = "none"
) -> dict[str, Any]:
    return {
        "settlementId": source["id"],
        "amount": money(cash),
        "forgivenessAmount": money(forgiven),
        "reason": "Correct an incorrectly entered original amount",
        "excessDisposition": excess,
        "categoryId": None,
    }


async def preview_correction(
    client: AsyncClient, base: str, body: dict[str, Any]
) -> dict[str, Any]:
    response = await client.post(f"{base}/settlement-corrections/preview", json=body)
    assert response.status_code == 200, response.text
    return dict(response.json())


def correction_headers(actor: User) -> dict[str, str]:
    return {**submission_headers(actor.id), "Finance-Command-Version": "2"}


@pytest.mark.parametrize("select_second", [False, True])
async def test_combined_can_restate_selected_debt_with_no_original_allocation(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    select_second: bool,
) -> None:
    client, actor, base, draft = advance_client
    first = await advance(client, actor, base, draft, "40.00", "2026-10-20")
    second = await advance(client, actor, base, draft, "60.00", "2026-10-20")
    first_id, second_id = first["obligation"]["id"], second["obligation"]["id"]
    selected = [second_id, first_id] if select_second else [first_id]
    source = await settle(client, actor, base, combined(draft, selected, "30.00", "5.00"))
    body = intent(source, "30.00", "20.00")
    if not select_second:
        rejected = await client.post(f"{base}/settlement-corrections/preview", json=body)
        assert rejected.status_code == 422 and "original selected scope" in rejected.text
        assert (await client.get(f"{base}/settlements/{source['id']}/corrections")).json() == []
        return
    plan = await preview_correction(client, base, body)
    assert plan["before"]["amount"] == plan["after"]["amount"] == money("30.00")
    assert plan["after"]["retainedCredit"] == money("0.00")
    assert [
        (a["position"], a["obligationId"], a["forgiven"]["amount"])
        for a in plan["after"]["allocations"]
        if "forgiven" in a
    ] == [(2, first_id, "10.00"), (3, second_id, "10.00")]
    assert [o["remaining"]["amount"] for o in plan["obligationsAfter"]] == ["0.00", "50.00"]
    result = await client.post(
        f"{base}/settlement-corrections",
        json={**body, "previewToken": plan["previewToken"]},
        headers=correction_headers(actor),
    )
    assert result.status_code == 201, result.text
    detail = (await client.get(f"{base}/settlements/{source['id']}")).json()
    assert detail["confirmationOrder"] == source["confirmationOrder"]
    assert detail["forgivenessAmount"] == money("5.00")
    assert detail["effectiveForgivenessAmount"] == money("20.00")
    assert [a["remainingAfter"] for a in detail["allocations"][:2]] == [
        money("10.00"),
        money("5.00"),
    ]
    assert [a["effectiveRemainingAfter"] for a in detail["allocations"][1:]] == [
        money("0.00"),
        money("50.00"),
    ]
    history = (await client.get(f"{base}/settlements/{source['id']}/corrections")).json()
    assert history[0]["after"] == plan["after"]
    assert {e["transactionDate"] for e in history[0]["financialEffectsAfter"]} == {
        source["transactionDate"]
    }
    for e in history[0]["financialEffectsAfter"]:
        assert (await client.delete(f"{base}/transactions/{e['transactionId']}")).status_code == 409


@pytest.mark.parametrize("legacy", [False, True])
async def test_selected_order_is_independent_of_later_unallocated_advance_date_correction(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    legacy: bool,
) -> None:
    client, actor, base, draft = advance_client
    first = await advance(client, actor, base, draft, "40.00", "2026-10-20")
    second = await advance(client, actor, base, draft, "60.00", "2026-10-21")
    third = await advance(client, actor, base, draft, "60.00", "2026-10-22")
    ids = [a["obligation"]["id"] for a in [first, second, third]]
    source = await settle(client, actor, base, combined(draft, ids, "30.00", "5.00"))
    if legacy:
        row = await postgres_session.get(FinanceSettlement, UUID(source["id"]))
        assert row is not None
        row.selected_allocation_order = None
        await postgres_session.commit()
    await correct_advance(
        client,
        actor,
        base,
        {
            **advance_correction(third, amount="60.00", share="60.00"),
            "ownerShare": money("0.00"),
            "transactionDate": "2026-10-19",
        },
    )
    body = intent(source, "30.00", "20.00")
    if legacy:
        response = await client.post(f"{base}/settlement-corrections/preview", json=body)
        assert response.status_code == 409 and "original order" in response.text
    else:
        plan = await preview_correction(client, base, body)
        assert plan["after"]["allocations"][-1]["obligationId"] == ids[1]


async def test_correction_keeps_selected_scope_and_skips_a_now_ineligible_unallocated_advance(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    first = await advance(client, actor, base, draft, "40.00", "2026-10-20")
    moved = await advance(client, actor, base, draft, "60.00", "2026-10-21")
    eligible = await advance(client, actor, base, draft, "20.00", "2026-10-22")
    await advance(client, actor, base, draft, "80.00", "2026-10-22")
    selected = [a["obligation"]["id"] for a in (first, moved, eligible)]
    source = await settle(client, actor, base, combined(draft, selected, "30.00", "5.00"))
    await correct_advance(
        client,
        actor,
        base,
        {
            **advance_correction(moved, amount="60.00", share="60.00"),
            "ownerShare": money("0.00"),
            "transactionDate": "2026-10-23",
        },
    )
    history_path = f"{base}/settlements/{source['id']}/corrections"
    rejected = await client.post(
        f"{base}/settlement-corrections/preview", json=intent(source, "30.00", "31.00")
    )
    assert rejected.status_code == 422
    assert (await client.get(history_path)).json() == []
    body = intent(source, "30.00", "15.00")
    plan = await preview_correction(client, base, body)
    assert plan["selectedObligationIds"] == source["selectedObligationIds"]
    assert [(a["obligationId"], a["forgiven"]) for a in plan["after"]["allocations"][1:]] == [
        (selected[0], money("10.00")),
        (selected[2], money("5.00")),
    ]
    assert plan["obligationsAfter"][1] == plan["obligationsBefore"][1]
    result = await client.post(
        f"{base}/settlement-corrections",
        json={**body, "previewToken": plan["previewToken"]},
        headers=correction_headers(actor),
    )
    assert result.status_code == 201, result.text
    assert (await client.get(history_path)).json()[0]["after"] == plan["after"]
    detail = (await client.get(f"{base}/settlements/{source['id']}")).json()
    assert detail["selectedObligationIds"] == source["selectedObligationIds"]


async def test_archived_receiving_account_allows_only_unchanged_cash_effects(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    debt = await advance(client, actor, base, draft, "40.00", "2026-10-20")
    source = await settle(
        client, actor, base, combined(draft, [debt["obligation"]["id"]], "30.00", "5.00")
    )
    account_id = source["receivingAccountId"]
    archived = await client.post(f"{base}/accounts/{account_id}/archive")
    assert archived.status_code == 200, archived.text
    endpoint = f"{base}/settlement-corrections"
    changed_cash = await client.post(f"{endpoint}/preview", json=intent(source, "31.00", "5.00"))
    assert changed_cash.status_code == 409
    assert changed_cash.json()["code"] == "finance_account_archived"
    body = intent(source, "30.00", "4.00")
    plan = await preview_correction(client, base, body)
    before_cash, after_cash = (
        [effect for effect in plan[side] if effect["kind"] != "expense"]
        for side in ("financialEffectsBefore", "financialEffectsAfter")
    )
    assert before_cash == after_cash and len(after_cash) == 1
    result = await client.post(
        endpoint,
        json={**body, "previewToken": plan["previewToken"]},
        headers=correction_headers(actor),
    )
    assert result.status_code == 201, result.text
    detail = (await client.get(f"{base}/settlements/{source['id']}")).json()
    assert detail["receivingAccountId"] == account_id
    assert detail["transactionIds"] == source["transactionIds"]
    assert detail["effectiveForgivenessAmount"] == money("4.00")


async def test_combined_cash_decrease_uses_reverse_confirmed_order_after_replacement(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
) -> None:
    client, actor, base, draft = advance_client
    first = await advance(client, actor, base, draft, "40.00", "2026-10-20")
    second = await advance(client, actor, base, draft, "60.00", "2026-10-21")
    ids = [first["obligation"]["id"], second["obligation"]["id"]]
    source = await settle(client, actor, base, combined(draft, ids))
    # Restate an Expense and keep its Allocation position when the linked Expense is recreated.
    first_body = intent(source, "90.00", "0.00")
    first_plan = await preview_correction(client, base, first_body)
    result = await client.post(
        f"{base}/settlement-corrections",
        json={**first_body, "previewToken": first_plan["previewToken"]},
        headers=correction_headers(actor),
    )
    assert result.status_code == 201, result.text
    body = intent(source, "70.00", "10.00")
    plan = await preview_correction(client, base, body)
    assert [
        (a["position"], a["obligationId"], a["cashPaid"]["amount"])
        for a in plan["after"]["allocations"]
    ] == [
        (1, ids[0], "40.00"),
        (2, ids[1], "30.00"),
        (3, ids[1], "0.00"),
    ]
    restored = plan["after"]["allocations"][2]
    assert restored["forgiven"] == money("10.00")
    assert restored["expenseTransactionId"] != source["transactionIds"][2]
    result = await client.post(
        f"{base}/settlement-corrections",
        json={**body, "previewToken": plan["previewToken"]},
        headers=correction_headers(actor),
    )
    assert result.status_code == 201, result.text
    assert (
        await postgres_session.get(FinanceTransaction, UUID(restored["expenseTransactionId"]))
        is not None
    )


@pytest.mark.parametrize("phase", ["rollback", "response_loss"])
async def test_standalone_correction_recovers_one_complete_original_date_expense(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    client, actor, base, draft = advance_client
    debt = await advance(client, actor, base, draft, "40.00", "2026-10-20")
    id = debt["obligation"]["id"]
    source = await settle(client, actor, base, forgiveness(draft, [id], "20.00"))
    body = intent(source, "0.00", "10.00")
    plan = await preview_correction(client, base, body)
    assert plan["receivingAccountId"] is None
    assert plan["obligationsAfter"][0]["remaining"] == money("30.00")
    assert [e["kind"] for e in plan["financialEffectsAfter"]] == ["expense"]
    command = {**body, "previewToken": plan["previewToken"]}
    headers = correction_headers(actor)
    original_flush = Session.flush

    def fail_after_effects(session: Session, objects: Any = None) -> None:
        has_expense = any(isinstance(row, FinanceCategoryAllocation) for row in session.new)
        original_flush(session, objects)
        if has_expense:
            raise RuntimeError("Expense flushed before grouped commit")

    original_send = ASGITransport.handle_async_request

    async def lose_response(transport: ASGITransport, request: Request) -> Response:
        result = await original_send(transport, request)
        assert result.status_code == 201, result.text
        assert await postgres_session.scalar(
            select(FinanceCategoryAllocation.amount)
            .join(
                FinanceTransaction,
                FinanceTransaction.id == FinanceCategoryAllocation.transaction_id,
            )
            .where(FinanceTransaction.id == UUID(source["transactionIds"][0]))
        ) == Decimal("10.00")
        await postgres_session.rollback()
        raise ReadError("committed response lost", request=request)

    endpoint = f"{base}/settlement-corrections"
    with monkeypatch.context() as fault:
        if phase == "rollback":
            fault.setattr(Session, "flush", fail_after_effects)
            failed = await client.post(endpoint, json=command, headers=headers)
            assert failed.status_code == 500 and "submissionReceipt" not in failed.json()
        else:
            fault.setattr(ASGITransport, "handle_async_request", lose_response)
            with pytest.raises(ReadError):
                await client.post(endpoint, json=command, headers=headers)
    committed = phase == "response_loss"
    assert await postgres_session.scalar(
        select(func.count()).select_from(FinanceSettlementCorrection)
    ) == int(committed)
    await postgres_session.rollback()
    await postgres_session.refresh(actor)
    if not committed:
        assert (await client.get(f"{base}/settlements/{source['id']}")).json() == source
    lookup = await client.get(
        f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
    )
    assert lookup.json()["state"] == ("terminal" if committed else "unfinished")
    result = await client.post(endpoint, json=command, headers=headers)
    assert result.status_code == 201, result.text
    assert (await client.post(endpoint, json=command, headers=headers)).json() == result.json()
    if committed:
        assert lookup.json()["receipt"] == result.json()
    later = await settle(
        client,
        actor,
        base,
        {
            **forgiveness(draft, [id], "2.00"),
            "transactionDate": "2026-11-01",
        },
    )
    assert later["confirmationOrder"] > source["confirmationOrder"]
    assert later["transactionDate"] == "2026-11-01"
    detail = (await client.get(f"{base}/settlements/{source['id']}")).json()
    assert detail["allocations"][0]["remainingAfter"] == money("20.00")
    assert detail["allocations"][0]["effectiveRemainingAfter"] == money("30.00")
    for month, expense in [("2026-10", "10.00"), ("2026-11", "2.00")]:
        summary = (await client.get(f"{base}/overview", params={"month": month})).json()
        assert summary["monthSummaryByCurrency"][0]["expense"] == money(expense)


@pytest.mark.parametrize("disposition", ["retainCredit", "acceptIncome"])
async def test_combined_reconfirms_stale_scope_and_reconciles_explicit_surplus(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    disposition: str,
) -> None:
    client, actor, base, draft = advance_client
    debt = await advance(client, actor, base, draft, "40.00", "2026-10-20")
    id = debt["obligation"]["id"]
    source = await settle(client, actor, base, combined(draft, [id], "30.00", "5.00"))
    body = intent(source, "50.00", "0.00", disposition)
    plan = await preview_correction(client, base, body)
    await confirm(client, actor, base, repayment(draft, "2.00"))
    endpoint = f"{base}/settlement-corrections"
    stale = await client.post(
        endpoint,
        json={**body, "previewToken": plan["previewToken"]},
        headers=correction_headers(actor),
    )
    assert (
        stale.status_code == 409
        and stale.json()["code"] == "finance_settlement_correction_preview_stale"
    )
    fresh = await preview_correction(client, base, body)
    assert fresh["after"][
        "retainedCredit" if disposition == "retainCredit" else "acceptedSurplus"
    ] == money("12.00")
    assert fresh["obligationsAfter"][0]["remaining"] == money("0.00")
    result = await client.post(
        endpoint,
        json={**body, "previewToken": fresh["previewToken"]},
        headers=correction_headers(actor),
    )
    assert result.status_code == 201, result.text
    detail = (await client.get(f"{base}/settlements/{source['id']}")).json()
    assert detail["effectiveCashAmount"] == money("50.00")
    assert detail["effectiveForgivenessAmount"] == money("0.00")
    if disposition == "retainCredit":
        origin = detail["creditOrigin"]
        assert origin["settlementId"] == source["id"]
        assert origin["confirmationOrder"] == source["confirmationOrder"]
        assert origin["available"] == money("12.00")
        liability = origin["liabilityAccountId"]
        receivable = debt["obligation"]["receivableAccountId"]
        cash = draft["fundingAccountId"]
        accounts = {a["id"]: a for a in (await client.get(f"{base}/accounts")).json()}
        assert accounts[liability]["currentBalance"] == money("12.00")
        assert accounts[receivable]["currentBalance"] == money("0.00")
        assert accounts[cash]["currentBalance"] == money("112.00")
        for account, nature, balance in [
            (liability, "liability", "12.00"),
            (receivable, "asset", "0.00"),
        ]:
            assert (
                await client.patch(f"{base}/accounts/{account}", json={"name": "Manual edit"})
            ).status_code == 409
            manual = await client.post(
                f"{base}/transactions",
                json={
                    "kind": "income",
                    "accountId": account,
                    "transactionDate": source["transactionDate"],
                    "economicAmount": money("1.00"),
                    "categoryAllocations": [{"amount": money("1.00"), "categoryId": None}],
                },
                headers=submission_headers(actor.id),
            )
            assert (
                manual.status_code == 409
                and manual.json()["code"] == "finance_linked_record_protected"
            )
            adjustment = await client.post(
                f"{base}/balance-adjustments",
                json={
                    "accountId": account,
                    "transactionDate": source["transactionDate"],
                    "expectedAccountNature": nature,
                    "expectedDerivedBalance": money(balance),
                    "targetBalance": money("1.00"),
                },
                headers=submission_headers(actor.id),
            )
            assert (
                adjustment.status_code == 409
                and adjustment.json()["code"] == "finance_linked_record_protected"
            )
        replacement = await client.put(
            f"{base}/transactions/{origin['transactionId']}",
            json={
                "kind": "internalTransfer",
                "sourceAccountId": liability,
                "destinationAccountId": cash,
                "transactionDate": source["transactionDate"],
                "amount": money("1.00"),
            },
        )
        assert (
            replacement.status_code == 409
            and replacement.json()["code"] == "finance_linked_record_protected"
        )
        newer = await retain_credit(client, actor, base, draft, "5.00", "2026-10-19")
        preview = await client.post(
            f"{base}/settlements/preview", json=credit_return(draft, "13.00")
        )
        assert preview.status_code == 200, preview.text
        assert [row["originSettlementId"] for row in preview.json()["origins"]] == [
            source["id"],
            newer["id"],
        ]
    else:
        assert detail["acceptedSurplus"] == money("12.00")
