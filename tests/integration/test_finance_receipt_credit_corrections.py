"""Correct a receipt with downstream applied Credit through the public Finance API."""

import asyncio
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient, ReadError, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from core_console.modules.finance import settlement_corrections
from core_console.modules.finance.advance_schemas import ObligationResponse
from core_console.modules.finance.advances import list_participant_obligations
from core_console.modules.finance.credit_application_models import (
    FinanceCreditApplicationAllocation,
)
from core_console.modules.finance.queries import get_finance_account_balance
from core_console.modules.finance.settlement_correction_models import (
    FinanceSettlementCorrectionTransaction,
)
from core_console.modules.users.models import User
from integration.finance_submission_helpers import submission_headers
from integration.test_finance_api import finance_client
from integration.test_finance_combined_settlements import combined
from integration.test_finance_credit_applications import application, apply_credit, retain_credit
from integration.test_finance_credit_returns import credit_return, return_credit
from integration.test_finance_forgiveness_settlement_corrections import (
    correction_headers,
    intent,
    preview_correction,
    settle,
)
from integration.test_finance_settlement_corrections import correct, correction, money
from integration.test_finance_settlements import advance, advance_client, confirm, repayment

pytestmark = pytest.mark.anyio
__all__ = ["advance_client"]


async def test_receipt_releases_last_direct_allocation_to_preserve_actual_return(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    first = await advance(client, actor, base, draft, "30.00", "2026-10-20")
    last = await advance(client, actor, base, draft, "50.00", "2026-10-21")
    origin = await retain_credit(client, actor, base, draft, "100.00")
    returned = await return_credit(client, actor, base, credit_return(draft, "20.00"))
    plan = await correct(client, actor, base, correction(origin, "90.00"))
    assert plan["after"]["amount"] == money("90.00")
    assert plan["after"]["retainedCredit"] == money("20.00")
    assert plan["after"]["acceptedSurplus"] == money("0.00")
    assert [a["cashPaid"] for a in plan["after"]["allocations"]] == [
        money("30.00"),
        money("40.00"),
    ]
    assert plan["creditCorrection"]["availableAfter"] == money("0.00")
    assert plan["creditCorrection"]["directCashReleasedForReturns"] == money("10.00")
    assert plan["creditCorrection"]["returns"][0]["settlementId"] == returned["id"]
    assert (await client.get(f"{base}/settlements/{returned['id']}")).json() == returned
    for advance_id, remaining in ((first["id"], "0.00"), (last["id"], "10.00")):
        debt = (await client.get(f"{base}/advances/{advance_id}")).json()["obligation"]
        assert debt["remaining"] == money(remaining)
    accounts = {a["id"]: a for a in (await client.get(f"{base}/accounts")).json()}
    assert accounts[str(draft["fundingAccountId"])]["currentBalance"] == money("90.00")
    [gross] = (await client.get(f"{base}/overview", params={"month": "2026-10"})).json()[
        "monthSummaryByCurrency"
    ]
    assert gross["income"] == gross["expense"] == money("0.00")
    rejected = await client.post(
        f"{base}/settlement-corrections/preview", json=correction(origin, "19.00")
    )
    assert rejected.status_code == 409 and "actual Credit returned" in rejected.json()["detail"]
    followup = await correct(client, actor, base, correction(origin, "60.00"))
    assert [a["cashPaid"] for a in followup["after"]["allocations"]] == [
        money("30.00"),
        money("10.00"),
    ]
    assert followup["before"] == plan["after"]
    assert followup["creditCorrection"]["returns"] == plan["creditCorrection"]["returns"]
    detail = (await client.get(f"{base}/settlements/{origin['id']}")).json()
    assert detail["allocations"][1]["remainingAfter"] == money("40.00")
    assert (await client.get(f"{base}/settlements/{origin['id']}/corrections")).json() == [
        plan,
        followup,
    ]


async def test_receipt_reduction_uses_available_credit_then_releases_newest_applications(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    origin = await retain_credit(client, actor, base, draft, "50.00")
    first = await advance(client, actor, base, draft, "12.00", "2026-10-23")
    last = await advance(client, actor, base, draft, "8.00", "2026-10-24")
    for advance_detail, amount in ((first, "12.00"), (last, "8.00")):
        await apply_credit(
            client, actor, base, application(draft, [advance_detail["obligation"]["id"]], amount)
        )
    returned = await return_credit(client, actor, base, credit_return(draft, "20.00"))
    for amount, available, applications in (
        ("45.00", "5.00", ["8.00", "12.00"]),
        ("32.00", "0.00", ["0.00", "12.00"]),
        ("20.00", "0.00", ["0.00", "0.00"]),
    ):
        plan = await correct(client, actor, base, correction(origin, amount, "retainCredit"))
        credit = plan["creditCorrection"]
        assert [item["appliedAfter"] for item in credit["applications"]] == [
            money(value) for value in applications
        ]
        assert credit["availableAfter"] == money(available)
        assert credit["directCashReleasedForReturns"] == money("0.00")
    assert (await client.get(f"{base}/settlements/{returned['id']}")).json() == returned


async def test_direct_cash_releases_only_after_affected_applications_are_exhausted(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    direct = await advance(client, actor, base, draft, "60.00", "2026-10-20")
    origin = await retain_credit(client, actor, base, draft, "100.00")
    later = await advance(client, actor, base, draft, "15.00", "2026-10-23")
    await apply_credit(
        client, actor, base, application(draft, [later["obligation"]["id"]], "15.00")
    )
    await return_credit(client, actor, base, credit_return(draft, "20.00"))

    first = await correct(client, actor, base, correction(origin, "85.00"))
    assert first["after"]["allocations"][0]["cashPaid"] == money("60.00")
    assert first["creditCorrection"]["applications"][0]["appliedAfter"] == money("5.00")

    second = await correct(client, actor, base, correction(origin, "75.00"))
    assert second["after"]["allocations"][0]["cashPaid"] == money("55.00")
    assert second["creditCorrection"]["applications"][0]["appliedAfter"] == money("0.00")
    assert second["creditCorrection"]["directCashReleasedForReturns"] == money("5.00")
    debts = {item["id"]: item for item in second["obligationsAfter"]}
    assert debts[direct["obligation"]["id"]]["remaining"] == money("5.00")
    assert debts[later["obligation"]["id"]]["remaining"] == money("15.00")


async def test_shared_return_preserves_both_original_fragments_and_other_origin(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    older = await retain_credit(client, actor, base, draft, "20.00")
    newer = await retain_credit(client, actor, base, draft, "30.00")
    returned = await return_credit(client, actor, base, credit_return(draft, "25.00"))
    older_path = f"{base}/settlements/{older['id']}"
    older_before = (await client.get(older_path)).json()
    plan = await correct(client, actor, base, correction(newer, "10.00", "retainCredit"))
    credit = plan["creditCorrection"]
    assert credit["availableAfter"] == money("5.00")
    [fragment] = credit["returns"]
    assert fragment["originAmount"] == money("5.00") and fragment["returnAmount"] == money("25.00")
    assert fragment["position"] == 2
    assert (await client.get(older_path)).json() == older_before
    assert (await client.get(f"{base}/settlements/{returned['id']}")).json() == returned
    blocked = await client.post(
        f"{base}/settlement-corrections/preview", json=correction(older, "18.00", "retainCredit")
    )
    assert blocked.status_code == 409


@pytest.mark.parametrize(
    "corrected, applied_after, available_after, remaining_after, cash_after",
    [
        ("25.00", "20.00", "5.00", "5.00", "100.00"),
        ("15.00", "15.00", "0.00", "10.00", "90.00"),
    ],
)
async def test_receipt_correction_preserves_or_releases_only_the_shortfall(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    corrected: str,
    applied_after: str,
    available_after: str,
    remaining_after: str,
    cash_after: str,
) -> None:
    client, actor, base, draft = advance_client
    origin = await retain_credit(client, actor, base, draft, "30.00")
    debt = await advance(client, actor, base, draft, "25.00", "2026-10-23")
    obligation = debt["obligation"]
    applied = await apply_credit(
        client, actor, base, application(draft, [obligation["id"]], "20.00")
    )
    body = correction(origin, corrected, "retainCredit")
    preview = await client.post(f"{base}/settlement-corrections/preview", json=body)
    assert preview.status_code == 200, preview.text
    result = await correct(client, actor, base, body)
    assert {key: result[key] for key in preview.json()} == preview.json()
    credit = result["creditCorrection"]
    assert credit["origin"] == origin["creditOrigin"] | {"available": money("10.00")}
    assert credit["availableAfter"] == money(available_after)
    [impact] = credit["applications"]
    assert impact["settlementId"] == applied["id"]
    assert impact["obligationId"] == obligation["id"]
    assert impact["originalApplied"] == impact["appliedBefore"] == money("20.00")
    assert impact["appliedAfter"] == money(applied_after)
    [current_debt] = result["obligationsAfter"]
    assert current_debt == (await client.get(f"{base}/advances/{debt['id']}")).json()["obligation"]
    assert current_debt["remaining"] == money(remaining_after)
    assert current_debt["creditApplied"] == money(applied_after)
    assert current_debt["cashPaid"] == current_debt["forgiven"] == money("0.00")
    participant = f"{base}/participants/{draft['participantId']}"
    [position] = (await client.get(f"{participant}/positions")).json()
    assert position["outstandingReceivables"] == money(remaining_after)
    assert position["availableCredits"] == money(available_after)
    assert (
        credit["participantPosition"]["outstandingReceivablesAfter"]
        == position["outstandingReceivables"]
    )
    assert credit["participantPosition"]["availableCreditAfter"] == position["availableCredits"]
    detail = (await client.get(f"{base}/settlements/{origin['id']}")).json()
    assert detail["amount"] == money(corrected)
    for field in ("id", "confirmedAt", "confirmationOrder", "transactionDate"):
        assert detail[field] == origin[field]
    assert detail["creditOrigin"] == origin["creditOrigin"] | {
        "amount": money(corrected),
        "available": money(available_after),
    }
    assert (await client.get(f"{base}/settlements/{origin['id']}/corrections")).json() == [result]
    application_detail = (await client.get(f"{base}/settlements/{applied['id']}")).json()
    assert application_detail["confirmationOrder"] == applied["confirmationOrder"]
    [allocation] = application_detail["allocations"]
    assert allocation["creditApplied"] == money("20.00")
    assert allocation.get("effectiveCreditApplied", allocation["creditApplied"]) == money(
        applied_after
    )
    accounts = {a["id"]: a for a in (await client.get(f"{base}/accounts")).json()}
    assert accounts[str(draft["fundingAccountId"])]["currentBalance"] == money(cash_after)
    assert accounts[obligation["receivableAccountId"]]["currentBalance"] == money(remaining_after)
    assert accounts[origin["creditOrigin"]["liabilityAccountId"]]["currentBalance"] == money(
        available_after
    )
    overview = (await client.get(f"{base}/overview", params={"month": "2026-10"})).json()
    [summary] = overview["monthSummaryByCurrency"]
    assert summary["income"] == summary["expense"] == money("0.00")
    [gross] = overview["financialPositionByCurrency"]
    assert gross["assetTotal"] == money("105.00" if corrected == "25.00" else "100.00")
    assert gross["liabilityTotal"] == money(available_after)


async def test_release_follows_confirmation_and_allocation_order_for_only_the_corrected_origin(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    origin = await retain_credit(client, actor, base, draft, "40.00", "2026-10-23")
    other_origin = await retain_credit(client, actor, base, draft, "24.00", "2026-10-22")
    first, second, third = [
        (await advance(client, actor, base, draft, value, day))["obligation"]["id"]
        for value, day in [
            ("10.00", "2026-10-20"),
            ("20.00", "2026-10-21"),
            ("30.00", "2026-10-22"),
        ]
    ]
    older = await apply_credit(
        client,
        actor,
        base,
        {**application(draft, [first], "10.00"), "transactionDate": "2026-10-28"},
    )
    newer = await apply_credit(
        client,
        actor,
        base,
        {**application(draft, [third, second], "35.00"), "transactionDate": "2026-10-26"},
    )
    result = await correct(client, actor, base, correction(origin, "25.00", "retainCredit"))
    credit = result["creditCorrection"]
    assert [
        (a["settlementId"], a["position"], a["obligationId"], a["released"])
        for a in credit["applications"]
    ] == [
        (newer["id"], 2, third, money("10.00")),
        (newer["id"], 1, second, money("5.00")),
        (older["id"], 1, first, money("0.00")),
    ]
    assert credit["availableAfter"] == money("0.00")
    assert credit["participantPosition"]["availableCreditAfter"] == money("19.00")
    assert credit["participantPosition"]["outstandingReceivablesAfter"] == money("30.00")
    assert {o["id"]: o["remaining"] for o in result["obligationsAfter"]} == {
        first: money("0.00"),
        second: money("5.00"),
        third: money("25.00"),
    }
    assert (await client.get(f"{base}/settlements/{older['id']}")).json() == older
    current = (await client.get(f"{base}/settlements/{newer['id']}")).json()
    assert current["origins"] == newer["origins"]
    assert current["allocations"] == [
        newer["allocations"][0] | {"effectiveCreditApplied": money("15.00")},
        newer["allocations"][1] | {"effectiveCreditApplied": money("0.00")},
        newer["allocations"][2],
    ]
    other_detail = (await client.get(f"{base}/settlements/{other_origin['id']}")).json()
    assert other_detail == other_origin | {
        "creditOrigin": other_origin["creditOrigin"] | {"available": money("19.00")}
    }
    # A repeat Correction still includes the receivable from the fully released fragment.
    reopened = next(o for o in result["obligationsAfter"] if o["id"] == third)
    retained_release = next(
        effect
        for effect in result["financialEffectsAfter"]
        if effect["transactionId"] == credit["applications"][0]["restatementTransactionId"]
    )
    next_body = correction(origin, "28.00", "retainCredit")
    next_preview = await preview_correction(client, base, next_body)
    next_result = await correct(client, actor, base, next_body)
    for plan in (next_preview, next_result):
        assert retained_release in plan["financialEffectsBefore"]
        assert retained_release in plan["financialEffectsAfter"]
        assert {
            "accountId": reopened["receivableAccountId"],
            "nature": "asset",
            "balanceBefore": money("25.00"),
            "balanceAfter": money("25.00"),
        } in plan["accountEffects"]
    assert all(
        a["released"] == money("0.00") for a in next_result["creditCorrection"]["applications"]
    )
    assert (await client.get(f"{base}/settlements/{newer['id']}")).json() == current
    reapplied = await apply_credit(client, actor, base, application(draft, [second], "4.00"))
    assert [(a["originSettlementId"], a["creditApplied"]) for a in reapplied["allocations"]] == [
        (origin["id"], money("3.00")),
        (other_origin["id"], money("1.00")),
    ]
    assert (await client.get(f"{base}/settlements/{origin['id']}/corrections")).json() == [
        result,
        next_result,
    ]


async def test_later_cash_receipt_restates_remaining_debt_after_credit_release(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    origin = await retain_credit(client, actor, base, draft, "30.00")
    debt = await advance(client, actor, base, draft, "100.00", "2026-10-23")
    applied = await apply_credit(
        client, actor, base, application(draft, [debt["obligation"]["id"]], "20.00")
    )
    receipt = await confirm(
        client, actor, base, repayment(draft, "10.00") | {"transactionDate": "2026-10-26"}
    )
    assert receipt["confirmationOrder"] > applied["confirmationOrder"]
    [allocation] = receipt["allocations"]
    assert allocation["remainingAfter"] == money("70.00")
    [cash_id] = receipt["transactionIds"]
    transaction_path = f"{base}/transactions/{cash_id}"
    cash_transaction = (await client.get(transaction_path)).json()

    await correct(client, actor, base, correction(origin, "15.00", "retainCredit"))

    receipt_detail = (await client.get(f"{base}/settlements/{receipt['id']}")).json()
    assert receipt_detail == receipt | {
        "allocations": [allocation | {"remainingAfter": money("75.00")}],
    }
    assert (await client.get(transaction_path)).json() == cash_transaction


@pytest.mark.parametrize("activity", ["application", "return"])
async def test_confirmation_rechecks_credit_activity_and_requires_a_fresh_plan(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    activity: str,
) -> None:
    client, actor, base, draft = advance_client
    origin = await retain_credit(client, actor, base, draft, "30.00")
    debt = await advance(client, actor, base, draft, "25.00", "2026-10-23")
    obligation = debt["obligation"]["id"]
    await apply_credit(client, actor, base, application(draft, [obligation], "15.00"))
    path = f"{base}/settlement-corrections"
    body = correction(origin, "10.00", "retainCredit")
    preview = await client.post(f"{path}/preview", json=body)
    assert preview.status_code == 200, preview.text
    if activity == "application":
        other = await advance(client, actor, base, draft, "8.00", "2026-10-23")
        latest = await apply_credit(
            client, actor, base, application(draft, [other["obligation"]["id"]], "5.00")
        )
        code = "finance_settlement_correction_preview_stale"
    else:
        returned = await return_credit(client, actor, base, credit_return(draft, "5.00"))
        assert returned["amount"] == money("5.00")
        code = "finance_settlement_correction_preview_stale"
    history_path = f"settlements/{origin['id']}/corrections"
    reads = ["accounts", "transactions", f"settlements/{origin['id']}", history_path]
    before = [(await client.get(f"{base}/{read}")).json() for read in reads]
    rejected = await client.post(
        path,
        json=body | {"previewToken": preview.json()["previewToken"]},
        headers=submission_headers(actor.id),
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["code"] == code
    assert rejected.json()["submissionReceipt"]["outcome"]["kind"] == "rejected"
    assert [(await client.get(f"{base}/{read}")).json() for read in reads] == before
    assert before[-1] == []
    if activity == "return":
        assert before[-2]["creditOrigin"]["hasReturnedCredit"] is True
        renewed = await correct(client, actor, base, body)
        assert renewed["previewToken"] != preview.json()["previewToken"]
        assert renewed["creditCorrection"]["applications"][0]["appliedAfter"] == money("5.00")
        assert (await client.get(f"{base}/settlements/{returned['id']}")).json() == returned
    else:
        renewed = await correct(client, actor, base, body)
        assert renewed["previewToken"] != preview.json()["previewToken"]
        assert renewed["creditCorrection"]["applications"][0]["settlementId"] == latest["id"]
        assert renewed["creditCorrection"]["applications"][0]["appliedAfter"] == money("0.00")


@pytest.mark.parametrize("returned", [False, True])
async def test_confirmation_records_stale_conflict_for_application_to_concurrent_new_debt(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
    returned: bool,
) -> None:
    client, actor, base, draft = advance_client
    origin = await retain_credit(client, actor, base, draft, "30.00")
    if returned:
        await return_credit(client, actor, base, credit_return(draft, "5.00"))
    body = correction(origin, "15.00", "retainCredit")
    preview = await preview_correction(client, base, body)
    command = body | {"previewToken": preview["previewToken"]}
    headers = submission_headers(actor.id)
    obligations_read, continue_confirmation = asyncio.Event(), asyncio.Event()

    async def pause_after_obligation_read(*args: Any, **kwargs: Any) -> list[ObligationResponse]:
        result = await list_participant_obligations(*args, **kwargs)
        if not obligations_read.is_set():
            obligations_read.set()
            await continue_confirmation.wait()
        return result

    with monkeypatch.context() as pause:
        pause.setattr(
            settlement_corrections, "list_participant_obligations", pause_after_obligation_read
        )
        async with asyncio.TaskGroup() as tasks:
            confirmation = tasks.create_task(
                client.post(f"{base}/settlement-corrections", json=command, headers=headers)
            )
            try:
                await asyncio.wait_for(obligations_read.wait(), timeout=8)
                debt = await advance(client, actor, base, draft, "25.00", "2026-10-23")
                applied = await apply_credit(
                    client, actor, base, application(draft, [debt["obligation"]["id"]], "20.00")
                )
            finally:
                continue_confirmation.set()

    rejected = confirmation.result()
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["code"] == "finance_settlement_correction_preview_stale"
    lookup = await client.get(
        f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
    )
    assert lookup.json()["state"] == "terminal"
    assert lookup.json()["receipt"] == rejected.json()["submissionReceipt"]
    assert (await client.get(f"{base}/settlements/{origin['id']}/corrections")).json() == []
    assert (await client.get(f"{base}/settlements/{applied['id']}")).json() == applied


async def test_combined_receipt_correction_reopens_debt_outside_the_receipts_cash_scope(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    first = await advance(client, actor, base, draft, "40.00", "2026-10-20")
    first_id = first["obligation"]["id"]
    source = await settle(client, actor, base, combined(draft, [first_id], "30.00", "5.00"))
    path = f"{base}/settlement-corrections"

    async def correct_combined(amount: str, disposition: str) -> dict[str, Any]:
        body = intent(source, amount, "0.00", disposition)
        plan = await preview_correction(client, base, body)
        response = await client.post(
            path,
            json=body | {"previewToken": plan["previewToken"]},
            headers=correction_headers(actor),
        )
        assert response.status_code == 201, response.text
        return plan

    initial = await correct_combined("60.00", "retainCredit")
    assert initial["after"]["retainedCredit"] == money("20.00")
    later = await advance(client, actor, base, draft, "20.00", "2026-10-23")
    later_id = later["obligation"]["id"]
    applied = await apply_credit(client, actor, base, application(draft, [later_id], "15.00"))
    result = await correct_combined("50.00", "retainCredit")
    assert result["after"]["retainedCredit"] == money("10.00")
    assert result["selectedObligationIds"] == [first_id]
    assert [o["id"] for o in result["obligationsAfter"]] == [first_id, later_id]
    assert [(o["remaining"], o["forgiven"]) for o in result["obligationsAfter"]] == [
        (money("0.00"), money("0.00")),
        (money("10.00"), money("0.00")),
    ]
    [impact] = result["creditCorrection"]["applications"]
    assert impact["settlementId"] == applied["id"]
    assert impact["released"] == money("5.00")

    final = await correct_combined("40.00", "none")
    assert final["after"]["retainedCredit"] == money("0.00")
    [impact] = final["creditCorrection"]["applications"]
    assert impact["originalApplied"] == money("15.00")
    assert impact["appliedBefore"] == impact["released"] == money("10.00")
    assert impact["appliedAfter"] == money("0.00")
    assert final["obligationsAfter"][1]["remaining"] == money("20.00")


async def test_restatement_protects_managed_positions_and_transactions(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    origin = await retain_credit(client, actor, base, draft, "20.00")
    debt = (await advance(client, actor, base, draft, "20.00", "2026-10-23"))["obligation"]
    applied = await apply_credit(client, actor, base, application(draft, [debt["id"]], "20.00"))
    result = await correct(client, actor, base, correction(origin, "10.00", "retainCredit"))
    [impact] = result["creditCorrection"]["applications"]
    entry = {
        "kind": "income",
        "accountId": draft["fundingAccountId"],
        "transactionDate": "2026-10-27",
        "economicAmount": money("1.00"),
        "categoryAllocations": [{"amount": money("1.00"), "categoryId": None}],
    }
    for id in (
        origin["creditOrigin"]["transactionId"],
        applied["transactionIds"][0],
        impact["restatementTransactionId"],
    ):
        path = f"{base}/transactions/{id}"
        for response in (await client.delete(path), await client.put(path, json=entry)):
            assert response.status_code == 409, response.text
            assert response.json()["code"] == "finance_linked_record_protected"
    for account, nature, balance in [
        (origin["creditOrigin"]["liabilityAccountId"], "liability", "0.00"),
        (debt["receivableAccountId"], "asset", "10.00"),
    ]:
        for response in (
            await client.patch(
                f"{base}/accounts/{account}", json={"openingBalance": money("1.00")}
            ),
            await client.post(
                f"{base}/transactions",
                json=entry | {"accountId": account},
                headers=submission_headers(actor.id),
            ),
            await client.post(
                f"{base}/balance-adjustments",
                json={
                    "accountId": account,
                    "transactionDate": "2026-10-27",
                    "expectedAccountNature": nature,
                    "expectedDerivedBalance": money(balance),
                    "targetBalance": money("1.00"),
                },
                headers=submission_headers(actor.id),
            ),
        ):
            assert response.status_code == 409, response.text
            assert response.json()["code"] == "finance_linked_record_protected"
    ordinary = await client.post(
        f"{base}/transactions", json=entry, headers=submission_headers(actor.id)
    )
    assert ordinary.status_code == 201, ordinary.text


@pytest.mark.parametrize("phase", ["rollback", "response_loss"])
@pytest.mark.parametrize("returned", [False, True])
async def test_receipt_credit_correction_recovers_one_atomic_outcome(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    returned: bool,
) -> None:
    client, actor, base, draft = advance_client
    origin = await retain_credit(client, actor, base, draft, "25.00" if returned else "20.00")
    debt = await advance(client, actor, base, draft, "25.00", "2026-10-23")
    allocation = await apply_credit(
        client, actor, base, application(draft, [debt["obligation"]["id"]], "20.00")
    )
    if returned:
        await return_credit(client, actor, base, credit_return(draft, "5.00"))
    path = f"{base}/settlement-corrections"
    body = correction(origin, "15.00" if returned else "10.00", "retainCredit")
    preview = await client.post(f"{path}/preview", json=body)
    assert preview.status_code == 200, preview.text
    command = body | {"previewToken": preview.json()["previewToken"]}
    headers = submission_headers(actor.id)
    ledger_id = UUID(origin["ledgerId"])

    async def observe(committed: bool) -> None:
        fragment = await postgres_session.get(
            FinanceCreditApplicationAllocation, (UUID(allocation["id"]), 1)
        )
        assert fragment is not None and fragment.amount == Decimal("20.00")
        assert fragment.effective_amount == (Decimal("10.00") if committed else None)
        for account_id, expected in [
            (draft["fundingAccountId"], "85.00" if committed else "95.00"),
            (origin["creditOrigin"]["liabilityAccountId"], "0.00"),
            (debt["obligation"]["receivableAccountId"], "15.00" if committed else "5.00"),
        ]:
            balance = await get_finance_account_balance(
                postgres_session,
                ledger_id=ledger_id,
                account_id=UUID(str(account_id)),
            )
            assert balance is not None and balance.current_balance == Decimal(expected)
        assert await postgres_session.scalar(
            select(func.count()).select_from(FinanceSettlementCorrectionTransaction)
        ) == (2 if committed else 0)
        await postgres_session.rollback()

    real_flush, real_handle = Session.flush, ASGITransport.handle_async_request

    def fail_after_release(session: Session, objects: Any = None) -> None:
        written = any(
            isinstance(row, FinanceSettlementCorrectionTransaction)
            and row.transaction_id
            == UUID(
                preview.json()["creditCorrection"]["applications"][0]["restatementTransactionId"]
            )
            for row in session.new
        )
        real_flush(session, objects)
        if written:
            raise RuntimeError("Injected failure after the Credit release was flushed")

    async def drop_committed_response(transport: ASGITransport, request: Request) -> Response:
        response = await real_handle(transport, request)
        assert response.status_code == 201, response.text
        await observe(committed=True)
        raise ReadError("Committed Correction response lost", request=request)

    with monkeypatch.context() as fault:
        if phase == "rollback":
            fault.setattr(Session, "flush", fail_after_release)
            failed = await client.post(path, json=command, headers=headers)
            assert failed.status_code == 500, failed.text
            await observe(committed=False)
            assert (await client.get(f"{base}/settlements/{origin['id']}/corrections")).json() == []
        else:
            fault.setattr(ASGITransport, "handle_async_request", drop_committed_response)
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
        if phase == "response_loss":
            assert result.json() == lookup.json()["receipt"]
        assert (await restarted.post(path, json=command, headers=headers)).json() == result.json()
        [history] = (await restarted.get(f"{base}/settlements/{origin['id']}/corrections")).json()
        assert {key: history[key] for key in preview.json()} == preview.json()
    await observe(committed=True)
