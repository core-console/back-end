"""Restate effective forgiveness and its Expense through the Advance correction API."""

from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient, ReadError, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core_console.modules.finance.advance_correction_models import (
    FinanceAdvanceCorrection,
    FinanceAdvanceCorrectionTransaction,
)
from core_console.modules.finance.models import FinanceCategoryAllocation
from core_console.modules.finance.settlement_models import (
    FinanceCreditOrigin,
    FinanceSettlementAllocation,
)
from core_console.modules.users.models import User
from integration.finance_submission_helpers import submission_headers
from integration.test_finance_advance_cash_corrections import share_correction
from integration.test_finance_advance_corrections import correct, money
from integration.test_finance_api import finance_client
from integration.test_finance_forgiveness import forgiveness
from integration.test_finance_nested_submission_persistence import _migrate
from integration.test_finance_settlements import advance, advance_client, confirm, repayment

pytestmark = pytest.mark.anyio
__all__ = ["advance_client"]


async def _settle(
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
    return cast(dict[str, Any], detail.json())


def _combined(draft: dict[str, object], source: dict[str, Any]) -> dict[str, Any]:
    return {
        **repayment(draft, "60.00"),
        **forgiveness(draft, [source["obligation"]["id"]], "60.00"),
        "kind": "combined",
        "forgivenessAmount": money("40.00"),
        "transactionDate": "2026-11-01",
    }


@pytest.mark.parametrize("kind", ["standalone", "combined"])
async def test_expense_dates_cash_and_forgiveness_reconcile_after_correction(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]], kind: str
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    if kind == "standalone":
        receipt = await confirm(client, actor, base, repayment(draft, "60.00"))
        settlement = await _settle(
            client,
            actor,
            base,
            {
                **forgiveness(draft, [source["obligation"]["id"]], "40.00"),
                "transactionDate": "2026-11-01",
            },
        )
    else:
        receipt = settlement = await _settle(client, actor, base, _combined(draft, source))
    expense_id = settlement["transactionIds"][-1]
    cash_id = receipt["transactionIds"][0]
    cash_url = f"{base}/transactions/{cash_id}"
    original_cash = (await client.get(cash_url)).json()
    path = f"{base}/settlements/{settlement['id']}"

    body = share_correction(source, "80.00", "20.00")
    preview = await client.post(f"{base}/advance-corrections/preview", json=body)
    assert preview.status_code == 200, preview.text
    plan = preview.json()
    [restatement] = plan["forgivenessRestatements"]
    assert restatement == {
        "settlementId": settlement["id"],
        "confirmationOrder": settlement["confirmationOrder"],
        "obligationId": source["obligation"]["id"],
        "position": settlement["allocations"][-1]["position"],
        "transactionDate": "2026-11-01",
        "expenseTransactionId": expense_id,
        "originalForgiven": money("40.00"),
        "forgivenBefore": money("40.00"),
        "forgivenAfter": money("20.00"),
    }
    assert (await client.get(path)).json() == settlement
    correction = await correct(client, actor, base, body)
    assert {key: correction[key] for key in plan} == plan
    assert correction.get("cashReleases", []) == []
    obligation = correction["after"]["obligations"][0]
    assert obligation["cashPaid"] == money("60.00")
    assert obligation["forgiven"] == money("20.00")
    assert obligation["remaining"] == money("0.00")
    assert (await client.get(cash_url)).json() == original_cash
    detail = (await client.get(path)).json()
    assert detail["allocations"][-1] == {
        **settlement["allocations"][-1],
        "effectiveForgiven": money("20.00"),
    }
    expenses = (await client.get(f"{base}/transactions", params={"kind": "expense"})).json()[
        "items"
    ]
    assert [e["transactionDate"] for e in expenses] == ["2026-11-01", "2026-10-20"]
    assert expenses[0]["id"] == expense_id
    for month in ("2026-10", "2026-11"):
        overview = (await client.get(f"{base}/overview", params={"month": month})).json()
        assert overview["monthSummaryByCurrency"][0]["expense"] == money("20.00")
    participant = f"{base}/participants/{draft['participantId']}"
    assert (await client.get(f"{participant}/credits")).json() == []
    accounts = {a["id"]: a for a in (await client.get(f"{base}/accounts")).json()}
    assert accounts[obligation["receivableAccountId"]]["currentBalance"] == money("0.00")
    assert accounts[draft["fundingAccountId"]]["currentBalance"] == money("60.00")

    reopened = await correct(client, actor, base, share_correction(source, "90.00", "10.00"))
    assert reopened.get("forgivenessRestatements", []) == []
    assert reopened["after"]["obligations"][0]["forgiven"] == money("20.00")
    assert reopened["after"]["obligations"][0]["remaining"] == money("10.00")

    released = await correct(client, actor, base, share_correction(source, "50.00", "50.00"))
    assert released["forgivenessRestatements"][0]["forgivenBefore"] == money("20.00")
    assert released["forgivenessRestatements"][0]["forgivenAfter"] == money("0.00")
    [release] = released["cashReleases"]
    assert release["settlementId"] == receipt["id"]
    assert release["confirmationOrder"] == receipt["confirmationOrder"]
    assert release["released"] == money("10.00")
    assert (await client.get(cash_url)).json() == original_cash
    assert (await client.get(f"{base}/transactions/{expense_id}")).status_code == 404
    assert (await client.get(path)).json()["allocations"][-1]["effectiveForgiven"] == money("0.00")
    current_receipt = (await client.get(f"{base}/settlements/{receipt['id']}")).json()
    assert current_receipt["amount"] == receipt["amount"] == money("60.00")
    assert current_receipt["creditOrigin"]["amount"] == money("10.00")
    assert current_receipt["creditOrigin"]["settlementId"] == receipt["id"]
    assert current_receipt["allocations"][0][
        "effectiveCashPaid" if kind == "combined" else "cashPaid"
    ] == money("50.00")

    increased = await correct(client, actor, base, share_correction(source, "75.00", "25.00"))
    assert increased.get("forgivenessRestatements", []) == increased.get("cashReleases", []) == []
    assert increased["after"]["obligations"][0]["remaining"] == money("25.00")
    [position] = (await client.get(f"{participant}/positions")).json()
    assert position["outstandingReceivables"] == money("25.00")
    assert position["availableCredits"] == money("10.00")
    assert (await client.get(f"{base}/advances/{source['id']}/corrections")).json() == [
        correction,
        reopened,
        released,
        increased,
    ]


async def test_unpaid_debt_first_then_newest_forgiveness_and_stale_preview_rejection(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    unrelated = await advance(client, actor, base, draft, "10.00", "2026-10-21")
    await confirm(client, actor, base, repayment(draft, "40.00"))
    ids = [source["obligation"]["id"]]
    first = await _settle(
        client, actor, base, {**forgiveness(draft, ids, "10.00"), "transactionDate": "2026-11-02"}
    )
    body = share_correction(source, "85.00", "15.00")
    old = (await client.post(f"{base}/advance-corrections/preview", json=body)).json()
    newest = await _settle(client, actor, base, forgiveness(draft, ids, "30.00"))
    other = await _settle(
        client, actor, base, forgiveness(draft, [unrelated["obligation"]["id"]], "5.00")
    )
    rejected = await client.post(
        f"{base}/advance-corrections",
        json={**body, "previewToken": old["previewToken"]},
        headers=submission_headers(actor.id),
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["code"] == "finance_advance_correction_preview_stale"
    assert (await client.get(f"{base}/advances/{source['id']}/corrections")).json() == []
    revised = await correct(client, actor, base, body)
    assert revised.get("forgivenessRestatements", []) == []
    assert revised["after"]["obligations"][0]["remaining"] == money("5.00")
    corrected = await correct(client, actor, base, share_correction(source, "45.00", "55.00"))
    assert [r["settlementId"] for r in corrected["forgivenessRestatements"]] == [
        newest["id"],
        first["id"],
    ]
    assert [r["forgivenAfter"] for r in corrected["forgivenessRestatements"]] == [
        money("0.00"),
        money("5.00"),
    ]
    assert corrected.get("cashReleases", []) == []
    assert (await client.get(f"{base}/settlements/{other['id']}")).json() == other


async def test_populated_t14_upgrade_preserves_finance_records_through_repeated_releases(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_engine: AsyncEngine,
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    combined = await _settle(client, actor, base, _combined(draft, source))
    cash_source = await advance(client, actor, base, draft, "30.00", "2026-10-19")
    cash_receipt = await confirm(client, actor, base, repayment(draft, "30.00"))
    cash_correction = await correct(
        client, actor, base, share_correction(cash_source, "18.00", "12.00")
    )
    other = await advance(client, actor, base, draft, "30.00", "2026-10-21")
    standalone = await _settle(
        client, actor, base, forgiveness(draft, [other["obligation"]["id"]], "5.00")
    )
    participant = f"participants/{draft['participantId']}"
    combined_path = f"settlements/{combined['id']}"
    standalone_path = f"settlements/{standalone['id']}"
    paths = [
        "accounts",
        "transactions",
        combined_path,
        standalone_path,
        f"settlements/{cash_receipt['id']}",
        f"advances/{cash_source['id']}/corrections",
        f"{participant}/credits",
        f"{participant}/positions",
    ]
    before = [(await client.get(f"{base}/{path}")).json() for path in paths]
    await _migrate(postgres_engine, revision="20261117_01", downgrade=True)
    async with postgres_engine.connect() as connection:
        stored_plan = await connection.scalar(
            select(FinanceAdvanceCorrection.plan).where(
                FinanceAdvanceCorrection.id == UUID(cash_correction["id"])
            )
        )
    assert stored_plan is not None and "forgivenessRestatements" not in stored_plan
    await _migrate(postgres_engine, revision="head", downgrade=False)
    assert [(await client.get(f"{base}/{path}")).json() for path in paths] == before

    first = await correct(client, actor, base, share_correction(source, "50.00", "50.00"))
    second = await correct(client, actor, base, share_correction(source, "35.00", "65.00"))
    [initial] = first["cashReleases"]
    [later] = second["cashReleases"]
    assert second.get("forgivenessRestatements", []) == []
    assert later["settlementId"] == initial["settlementId"] == combined["id"]
    assert later["confirmationOrder"] == combined["confirmationOrder"]
    assert later["transactionDate"] == initial["transactionDate"] == combined["transactionDate"]
    assert later["creditAccountId"] == initial["creditAccountId"]
    assert later["cashBefore"] == money("50.00")
    assert later["cashAfter"] == money("35.00")
    assert later["creditBefore"] == money("10.00")
    assert later["creditAfter"] == money("25.00")

    detail = (await client.get(f"{base}/{combined_path}")).json()
    assert detail["amount"] == money("60.00")
    assert detail["allocations"] == [
        {
            **combined["allocations"][0],
            "effectiveCashPaid": money("35.00"),
            "effectiveRemainingAfter": money("0.00"),
        },
        {**combined["allocations"][1], "effectiveForgiven": money("0.00")},
    ]
    assert detail["transactionIds"] == [
        combined["transactionIds"][0],
        initial["financialEffect"]["transactionId"],
        later["financialEffect"]["transactionId"],
    ]
    assert (await client.get(f"{base}/{standalone_path}")).json() == standalone
    credits = (await client.get(f"{base}/{participant}/credits")).json()
    assert credits == [
        detail["creditOrigin"],
        (await client.get(f"{base}/settlements/{cash_receipt['id']}")).json()["creditOrigin"],
    ]
    assert detail["creditOrigin"]["available"] == money("25.00")
    [position] = (await client.get(f"{base}/{participant}/positions")).json()
    assert position["outstandingReceivables"] == money("25.00")
    assert position["availableCredits"] == money("37.00")
    accounts = {a["id"]: a for a in (await client.get(f"{base}/accounts")).json()}
    assert accounts[later["creditAccountId"]]["currentBalance"] == money("25.00")
    expenses = (await client.get(f"{base}/transactions", params={"kind": "expense"})).json()[
        "items"
    ]
    assert [(e["transactionDate"], e["economicAmount"]) for e in expenses] == [
        (standalone["transactionDate"], money("5.00")),
        (source["transactionDate"], money("65.00")),
        (cash_source["transactionDate"], money("12.00")),
    ]


@pytest.mark.parametrize("phase", ["rollback", "response_loss"])
async def test_forgiveness_and_credit_recover_as_one_correction(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    settlement = await _settle(client, actor, base, _combined(draft, source))
    settlement_id = UUID(settlement["id"])
    expense_id = UUID(settlement["transactionIds"][-1])
    body = share_correction(source, "50.00", "50.00")
    preview = (await client.post(f"{base}/advance-corrections/preview", json=body)).json()
    command = {**body, "previewToken": preview["previewToken"]}
    headers = submission_headers(actor.id)
    real_flush = AsyncSession.flush
    real_handle = ASGITransport.handle_async_request

    async def assert_effects(committed: bool) -> None:
        allocation = await postgres_session.get(FinanceSettlementAllocation, (settlement_id, 2))
        assert allocation is not None and allocation.forgiven == Decimal("40.00")
        assert allocation.effective_forgiven == (Decimal(0) if committed else None)
        assert await postgres_session.scalar(
            select(FinanceCategoryAllocation.amount).where(
                FinanceCategoryAllocation.transaction_id == expense_id,
            )
        ) == (None if committed else Decimal("40.00"))
        origin = await postgres_session.get(FinanceCreditOrigin, settlement_id)
        assert (origin.amount if origin else None) == (Decimal("10.00") if committed else None)
        count = await postgres_session.scalar(
            select(func.count()).select_from(FinanceAdvanceCorrection)
        )
        assert count == int(committed)
        await postgres_session.rollback()

    async def fail_after_release(session: AsyncSession, objects: Any = None) -> None:
        releasing = any(isinstance(row, FinanceAdvanceCorrectionTransaction) for row in session.new)
        await real_flush(session, objects)
        if releasing:
            raise RuntimeError("injected failure after forgiveness and cash restatement")

    async def drop_response(transport: ASGITransport, request: Request) -> Response:
        response = await real_handle(transport, request)
        assert response.status_code == 201, response.text
        await assert_effects(committed=True)
        raise ReadError("committed response lost", request=request)

    with monkeypatch.context() as fault:
        if phase == "rollback":
            fault.setattr(AsyncSession, "flush", fail_after_release)
            result = await client.post(f"{base}/advance-corrections", json=command, headers=headers)
            assert result.status_code == 500, result.text
            await assert_effects(committed=False)
        else:
            fault.setattr(ASGITransport, "handle_async_request", drop_response)
            with pytest.raises(ReadError):
                await client.post(f"{base}/advance-corrections", json=command, headers=headers)

    await postgres_session.refresh(actor)
    async with finance_client(database_url=postgres_database_url, actor=actor) as restarted:
        lookup = await restarted.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
        )
        assert lookup.json()["state"] == ("unfinished" if phase == "rollback" else "terminal")
        result = await restarted.post(f"{base}/advance-corrections", json=command, headers=headers)
        assert result.status_code == 201, result.text
        replay = await restarted.post(f"{base}/advance-corrections", json=command, headers=headers)
        assert replay.json() == result.json()
    await assert_effects(committed=True)
