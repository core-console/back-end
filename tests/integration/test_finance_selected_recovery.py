"""Atomic selected-scope effects and response loss with real PostgreSQL observations."""

from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient, ReadError, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance.models import FinanceAccountMovement, FinanceTransaction
from core_console.modules.finance.settlement_models import (
    FinanceCreditOrigin,
    FinanceSettlement,
    FinanceSettlementAllocation,
    FinanceSettlementIncome,
)
from core_console.modules.finance.submission_models import FinanceSubmission
from core_console.modules.users.models import User
from integration.finance_submission_helpers import submission_headers
from integration.test_finance_api import finance_client
from integration.test_finance_settlements import advance, advance_client, repayment

pytestmark = pytest.mark.anyio
__all__ = ["advance_client"]


@pytest.mark.parametrize(
    "disposition", ["repayOnly", "retainCredit", "acceptIncome", "forgiveness"]
)
@pytest.mark.parametrize(
    "phase", ["partial_write", "before_commit", "admission_ack", "terminal_ack", "response_loss"]
)
async def test_selected_group_recovers_original_scope_and_all_financial_effects(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    disposition: str,
    phase: str,
) -> None:
    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "40.00", "2026-10-20")
    second = await advance(client, actor, base, draft, "60.00", "2026-10-21")
    selected = [second["obligation"]["id"]]
    body = {
        **repayment(draft, "50.00" if disposition == "repayOnly" else "80.00"),
        "selectedObligationIds": selected,
    }
    if disposition not in ("repayOnly", "forgiveness"):
        body["excessDisposition"] = disposition
    if disposition == "acceptIncome":
        body["categoryAllocations"] = [{"amount": {"amount": "20.00", "currency": "CNY"}}]
    if disposition == "forgiveness":
        body = {
            "kind": "forgiveness",
            "participantId": draft["participantId"],
            "selectedObligationIds": selected,
            "transactionDate": "2026-10-22",
            "amount": {"amount": "50.00", "currency": "CNY"},
            "reason": "Waive selected debt",
        }
    preview = await client.post(f"{base}/settlements/preview", json=body)
    assert preview.status_code == 200, preview.text
    command = {**body, "previewToken": preview.json()["previewToken"]}
    headers = {
        **submission_headers(actor.id),
        "Finance-Command-Version": "5" if disposition == "forgiveness" else "4",
    }
    real_commit, real_flush = AsyncSession.commit, AsyncSession.flush
    commits = 0

    async def commit_fault(session: AsyncSession) -> None:
        nonlocal commits
        commits += 1
        if commits == 2 and phase == "before_commit":
            raise ConnectionError("terminal commit never reached PostgreSQL")
        await real_commit(session)
        if commits == (1 if phase == "admission_ack" else 2):
            raise ConnectionError("commit acknowledgement lost")

    async def flush_fault(session: AsyncSession, objects: Any = None) -> None:
        partial = any(isinstance(row, FinanceSettlementAllocation) for row in session.new)
        await real_flush(session, objects)
        if partial:
            raise RuntimeError("selected principal written before group failure")

    handle = ASGITransport.handle_async_request

    async def drop_response(transport: ASGITransport, request: Request) -> Response:
        response = await handle(transport, request)
        assert response.status_code == 201, response.text
        # Independent observation precedes delivery loss; no fixture success substitutes for commit.
        binding = await postgres_session.get(
            FinanceSubmission, (actor.id, UUID(headers["Idempotency-Key"]))
        )
        assert binding is not None and binding.terminal_outcome is not None
        saved = await postgres_session.scalar(select(FinanceSettlement))
        assert saved is not None and saved.selected_obligation_ids == [UUID(selected[0])]
        await postgres_session.rollback()
        raise ReadError("committed selected response lost", request=request)

    with monkeypatch.context() as faults:
        if phase == "response_loss":
            faults.setattr(ASGITransport, "handle_async_request", drop_response)
            with pytest.raises(ReadError):
                await client.post(f"{base}/settlements", json=command, headers=headers)
        else:
            faults.setattr(
                AsyncSession,
                "flush" if phase == "partial_write" else "commit",
                flush_fault if phase == "partial_write" else commit_fault,
            )
            assert (
                await client.post(f"{base}/settlements", json=command, headers=headers)
            ).status_code == 500
    committed = phase in ("terminal_ack", "response_loss")
    counts = [
        await postgres_session.scalar(select(func.count()).select_from(model))
        for model in (
            FinanceSettlement,
            FinanceSettlementAllocation,
            FinanceCreditOrigin,
            FinanceSettlementIncome,
            FinanceTransaction,
            FinanceAccountMovement,
        )
    ]
    expected = {
        "repayOnly": [1, 1, 0, 0, 3, 6],
        "forgiveness": [1, 1, 0, 0, 3, 5],
        "retainCredit": [1, 1, 1, 0, 4, 8],
        "acceptIncome": [1, 1, 0, 1, 4, 7],
    }[disposition]
    assert counts == (expected if committed else [0, 0, 0, 0, 2, 4])
    await postgres_session.rollback()
    await postgres_session.refresh(actor)
    async with finance_client(database_url=postgres_database_url, actor=actor) as restarted:
        lookup = await restarted.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
        )
        assert lookup.json()["state"] == ("terminal" if committed else "unfinished")
        retry = await restarted.post(f"{base}/settlements", json=command, headers=headers)
        assert retry.status_code == 201, retry.text
        if committed:
            assert retry.json() == lookup.json()["receipt"]
        assert (
            await restarted.post(f"{base}/settlements", json=command, headers=headers)
        ).json() == retry.json()
        detail = (
            await restarted.get(f"{base}/settlements/{retry.json()['outcome']['resource']['id']}")
        ).json()
        assert detail["selectedObligationIds"] == selected
        obligations = (
            await restarted.get(f"{base}/participants/{draft['participantId']}/obligations")
        ).json()
        assert [row["remaining"]["amount"] for row in obligations] == [
            "40.00",
            "10.00" if disposition in ("repayOnly", "forgiveness") else "0.00",
        ]
    effects = (
        await postgres_session.scalars(
            select(FinanceAccountMovement.amount).where(
                FinanceAccountMovement.account_id == UUID(str(draft["fundingAccountId"]))
            )
        )
    ).all()
    assert sum(effects) == Decimal(
        "-100.00"
        if disposition == "forgiveness"
        else "-50.00"
        if disposition == "repayOnly"
        else "-20.00"
    )
    if disposition == "forgiveness":
        assert detail["kind"] == "forgiveness"
        assert obligations[1]["forgiven"]["amount"] == "50.00"
        assert obligations[1]["cashPaid"]["amount"] == "0.00"
        overview = (await client.get(f"{base}/overview", params={"month": "2026-10"})).json()
        assert overview["monthSummaryByCurrency"][0]["expense"]["amount"] == "50.00"
    assert [
        await postgres_session.scalar(select(func.count()).select_from(model))
        for model in (
            FinanceSettlement,
            FinanceSettlementAllocation,
            FinanceCreditOrigin,
            FinanceSettlementIncome,
            FinanceTransaction,
            FinanceAccountMovement,
        )
    ] == expected
