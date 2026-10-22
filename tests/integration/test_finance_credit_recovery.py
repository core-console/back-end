"""Atomic credit groups recover against the dedicated real PostgreSQL target."""

import asyncio
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient, ReadError, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance.models import (
    FinanceAccount,
    FinanceAccountMovement,
    FinanceTransaction,
)
from core_console.modules.finance.settlement_models import (
    FinanceCreditOrigin,
    FinanceSettlement,
    FinanceSettlementAllocation,
)
from core_console.modules.finance.submission_models import FinanceSubmission
from core_console.modules.users.models import User
from integration.finance_submission_helpers import submission_headers
from integration.test_finance_advances import advance_client as advance_client
from integration.test_finance_api import finance_client
from integration.test_finance_credits import retained
from integration.test_finance_settlements import advance

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("debt", [False, True])
@pytest.mark.parametrize(
    "phase", ["credit_flush", "before_commit", "admission_ack", "terminal_ack", "response_loss"]
)
async def test_credit_group_rollback_and_committed_response_recovery(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    debt: bool,
    phase: str,
) -> None:
    client, actor, base, draft = advance_client
    if debt:
        await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = retained(draft, "102.00" if debt else "50.00")
    preview = await client.post(f"{base}/settlements/preview", json=body)
    command = {**body, "previewToken": preview.json()["previewToken"]}
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "2"}
    real_commit, real_flush = AsyncSession.commit, AsyncSession.flush
    commits = 0

    async def commit_fault(session: AsyncSession) -> None:
        nonlocal commits
        commits += 1
        if commits == 2 and phase == "before_commit":
            raise ConnectionError("terminal commit never reached PostgreSQL")
        await real_commit(session)
        if commits == (1 if phase == "admission_ack" else 2):
            raise ConnectionError("PostgreSQL acknowledgement lost")

    async def flush_fault(session: AsyncSession, objects: Any = None) -> None:
        credit_written = any(isinstance(row, FinanceCreditOrigin) for row in session.new)
        await real_flush(session, objects)
        if credit_written:
            raise RuntimeError("credit origin, Liability and cash effects flushed before failure")

    handle = ASGITransport.handle_async_request

    async def drop_response(transport: ASGITransport, request: Request) -> Response:
        response = await handle(transport, request)
        assert response.status_code == 201, response.text
        # Observe the commit on a separate PostgreSQL session before losing delivery.
        assert (
            await postgres_session.scalar(select(func.count()).select_from(FinanceCreditOrigin))
            == 1
        )
        binding = await postgres_session.get(
            FinanceSubmission, (actor.id, UUID(headers["Idempotency-Key"]))
        )
        assert binding is not None and binding.terminal_outcome is not None
        await postgres_session.rollback()
        raise ReadError("committed response lost", request=request)

    with monkeypatch.context() as faults:
        if phase == "response_loss":
            faults.setattr(ASGITransport, "handle_async_request", drop_response)
            with pytest.raises(ReadError):
                await client.post(f"{base}/settlements", json=command, headers=headers)
        else:
            faults.setattr(
                AsyncSession,
                "flush" if phase == "credit_flush" else "commit",
                flush_fault if phase == "credit_flush" else commit_fault,
            )
            failed = await client.post(f"{base}/settlements", json=command, headers=headers)
            assert failed.status_code == 500, failed.text
            assert "submissionReceipt" not in failed.json()
            assert "commandValidationRejection" not in failed.json()
    committed = phase in ("terminal_ack", "response_loss")
    counts = [
        await postgres_session.scalar(select(func.count()).select_from(model))
        for model in (
            FinanceSettlement,
            FinanceCreditOrigin,
            FinanceSettlementAllocation,
            FinanceAccount,
            FinanceTransaction,
            FinanceAccountMovement,
        )
    ]
    source_count = 1 if debt else 0
    assert counts == [
        int(committed),
        int(committed),
        int(committed and debt),
        1 + source_count + int(committed),
        source_count + ((1 + source_count) if committed else 0),
        2 * (source_count + ((1 + source_count) if committed else 0)),
    ]
    await postgres_session.rollback()
    await postgres_session.refresh(actor)
    async with finance_client(database_url=postgres_database_url, actor=actor) as restarted:
        lookup = await restarted.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
        )
        assert lookup.json()["state"] == ("terminal" if committed else "unfinished")
        assert (lookup.json()["receipt"] if committed else lookup.json())["commandVersion"] == "2"
        assert lookup.headers["cache-control"] == "no-store"
        retry = await restarted.post(f"{base}/settlements", json=command, headers=headers)
        assert retry.status_code == 201, retry.text
        if committed:
            assert retry.json() == lookup.json()["receipt"]
        assert (
            await restarted.post(f"{base}/settlements", json=command, headers=headers)
        ).json() == retry.json()
        origins = (
            await restarted.get(f"{base}/participants/{draft['participantId']}/credits")
        ).json()
        assert len(origins) == 1
        assert origins[0]["amount"]["amount"] == ("2.00" if debt else "50.00")
    effects = (
        await postgres_session.scalars(
            select(FinanceAccountMovement.amount).where(
                FinanceAccountMovement.account_id == UUID(str(draft["fundingAccountId"]))
            )
        )
    ).all()
    assert sum(effects) == Decimal("2.00" if debt else "50.00")


@pytest.mark.parametrize("same_key", [True, False])
async def test_credit_confirmation_serializes_and_changed_preview_needs_reconfirmation(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]], same_key: bool
) -> None:
    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = retained(draft)
    preview = await client.post(f"{base}/settlements/preview", json=body)
    command = {**body, "previewToken": preview.json()["previewToken"]}
    first = {**submission_headers(actor.id), "Finance-Command-Version": "2"}
    second = first if same_key else {**submission_headers(actor.id), "Finance-Command-Version": "2"}
    responses = await asyncio.gather(
        *[client.post(f"{base}/settlements", json=command, headers=h) for h in (first, second)]
    )
    for index, response in enumerate(responses):
        if response.status_code == 503 or response.json().get("code") == "finance_submission_busy":
            responses[index] = await client.post(
                f"{base}/settlements", json=command, headers=(first, second)[index]
            )
    if same_key:
        assert responses[0].status_code == responses[1].status_code == 201
        assert responses[0].json() == responses[1].json()
    else:
        assert sorted(r.status_code for r in responses) == [201, 409]
        rejected = next(r for r in responses if r.status_code == 409)
        assert rejected.json()["code"] == "finance_settlement_preview_stale"
        assert rejected.json()["submissionReceipt"]["commandVersion"] == "2"
        refreshed = await client.post(f"{base}/settlements/preview", json=body)
        assert refreshed.json()["retainedCredit"]["amount"] == "102.00"
        assert refreshed.json()["previewToken"] != command["previewToken"]
    assert (
        len((await client.get(f"{base}/participants/{draft['participantId']}/credits")).json()) == 1
    )
