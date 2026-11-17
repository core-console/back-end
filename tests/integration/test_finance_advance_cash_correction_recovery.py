"""Recovery covers the released Allocation and its linked credit together."""

from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient, ReadError, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance.advance_correction_models import (
    FinanceAdvanceCorrectionTransaction,
)
from core_console.modules.finance.settlement_models import (
    FinanceCreditOrigin,
    FinanceSettlementAllocation,
)
from core_console.modules.finance.submission_models import FinanceSubmission
from core_console.modules.users.models import User
from integration.finance_submission_helpers import submission_headers
from integration.test_finance_advance_cash_corrections import share_correction
from integration.test_finance_api import finance_client
from integration.test_finance_settlements import advance, advance_client, confirm, repayment

pytestmark = pytest.mark.anyio
__all__ = ["advance_client"]


@pytest.mark.parametrize("phase", ["rollback", "response_loss"])
async def test_release_recovers_one_atomic_outcome(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "90.00", "2026-10-20")
    receipt = await confirm(client, actor, base, repayment(draft, "90.00"))
    body = share_correction(source, "60.00", "30.00")
    plan = (await client.post(f"{base}/advance-corrections/preview", json=body)).json()
    command = {**body, "previewToken": plan["previewToken"]}
    headers = submission_headers(actor.id)
    real_flush = AsyncSession.flush
    real_handle = ASGITransport.handle_async_request

    async def fail_after_release(session: AsyncSession, objects: Any = None) -> None:
        releasing = any(isinstance(row, FinanceAdvanceCorrectionTransaction) for row in session.new)
        await real_flush(session, objects)
        if releasing:
            raise RuntimeError("injected failure after cash release effects")

    async def drop_response(transport: ASGITransport, request: Request) -> Response:
        response = await real_handle(transport, request)
        assert response.status_code == 201, response.text
        assert (
            await postgres_session.scalar(
                select(func.count()).select_from(FinanceAdvanceCorrectionTransaction)
            )
            == 1
        )
        allocation = await postgres_session.get(
            FinanceSettlementAllocation, (UUID(receipt["id"]), 1)
        )
        origin = await postgres_session.get(FinanceCreditOrigin, UUID(receipt["id"]))
        retained = await postgres_session.get(
            FinanceSubmission, (actor.id, UUID(headers["Idempotency-Key"]))
        )
        assert allocation is not None and allocation.effective_cash_paid == Decimal("60.00")
        assert origin is not None and origin.amount == Decimal("30.00")
        assert retained is not None and retained.terminal_outcome is not None
        await postgres_session.rollback()
        raise ReadError("committed response lost", request=request)

    with monkeypatch.context() as fault:
        if phase == "rollback":
            fault.setattr(AsyncSession, "flush", fail_after_release)
            failed = await client.post(f"{base}/advance-corrections", json=command, headers=headers)
            assert failed.status_code == 500, failed.text
        else:
            fault.setattr(ASGITransport, "handle_async_request", drop_response)
            with pytest.raises(ReadError):
                await client.post(f"{base}/advance-corrections", json=command, headers=headers)

    count = await postgres_session.scalar(
        select(func.count()).select_from(FinanceAdvanceCorrectionTransaction)
    )
    assert count == (0 if phase == "rollback" else 1)
    await postgres_session.rollback()
    await postgres_session.refresh(actor)
    async with finance_client(database_url=postgres_database_url, actor=actor) as restarted:
        lookup = await restarted.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
        )
        assert lookup.json()["state"] == ("unfinished" if phase == "rollback" else "terminal")
        result = await restarted.post(f"{base}/advance-corrections", json=command, headers=headers)
        assert result.status_code == 201, result.text
        assert (
            await restarted.post(f"{base}/advance-corrections", json=command, headers=headers)
        ).json() == result.json()
        history = (await restarted.get(f"{base}/advances/{source['id']}/corrections")).json()
        assert len(history) == 1
        assert history[0]["cashReleases"][0]["released"]["amount"] == "30.00"
