"""Credit applications recover one complete financial outcome after faults."""

import asyncio
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient, ReadError, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance.credit_application_models import (
    FinanceCreditApplicationAllocation,
)
from core_console.modules.finance.submission_models import FinanceSubmission
from core_console.modules.users.models import User
from integration.finance_submission_helpers import submission_headers
from integration.test_finance_api import finance_client
from integration.test_finance_credit_applications import application, retain_credit
from integration.test_finance_settlements import advance, advance_client

pytestmark = pytest.mark.anyio
__all__ = ["advance_client"]


@pytest.mark.parametrize(
    "phase", ["fragment_flush", "before_commit", "admission_ack", "terminal_ack", "response_loss"]
)
async def test_application_rollback_and_restart_recover_all_origin_and_debt_effects(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    client, actor, base, draft = advance_client
    await retain_credit(client, actor, base, draft, "20.00")
    await retain_credit(client, actor, base, draft, "30.00")
    debt = await advance(client, actor, base, draft, "40.00", "2026-10-21")
    body = application(draft, [debt["obligation"]["id"]], "25.00")
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    command = {**body, "previewToken": preview["previewToken"]}
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "7"}
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
        written = any(isinstance(row, FinanceCreditApplicationAllocation) for row in session.new)
        await real_flush(session, objects)
        if written:
            raise RuntimeError("origin consumption and linked transfer flushed before failure")

    handle = ASGITransport.handle_async_request

    async def drop_response(transport: ASGITransport, request: Request) -> Response:
        response = await handle(transport, request)
        assert response.status_code == 201, response.text
        assert (
            await postgres_session.scalar(
                select(func.count()).select_from(FinanceCreditApplicationAllocation)
            )
            == 2
        )
        binding = await postgres_session.get(
            FinanceSubmission, (actor.id, UUID(headers["Idempotency-Key"]))
        )
        assert binding is not None and binding.terminal_outcome is not None
        await postgres_session.rollback()
        raise ReadError("committed response lost", request=request)

    paths = [
        "accounts",
        "transactions",
        f"participants/{draft['participantId']}/obligations",
        f"participants/{draft['participantId']}/credits",
    ]
    before = [(await client.get(f"{base}/{path}")).json() for path in paths]
    with monkeypatch.context() as faults:
        if phase == "response_loss":
            faults.setattr(ASGITransport, "handle_async_request", drop_response)
            with pytest.raises(ReadError):
                await client.post(f"{base}/settlements", json=command, headers=headers)
        else:
            faults.setattr(
                AsyncSession,
                "flush" if phase == "fragment_flush" else "commit",
                flush_fault if phase == "fragment_flush" else commit_fault,
            )
            failed = await client.post(f"{base}/settlements", json=command, headers=headers)
            assert failed.status_code == 500, failed.text
            assert "submissionReceipt" not in failed.json()
            assert "commandValidationRejection" not in failed.json()
    committed = phase in ("terminal_ack", "response_loss")
    assert await postgres_session.scalar(
        select(func.count()).select_from(FinanceCreditApplicationAllocation)
    ) == (2 if committed else 0)
    await postgres_session.rollback()
    if not committed:
        assert [(await client.get(f"{base}/{path}")).json() for path in paths] == before
    await postgres_session.refresh(actor)
    async with finance_client(database_url=postgres_database_url, actor=actor) as restarted:
        lookup = await restarted.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
        )
        assert lookup.json()["state"] == ("terminal" if committed else "unfinished")
        assert lookup.headers["cache-control"] == "no-store"
        result = await restarted.post(f"{base}/settlements", json=command, headers=headers)
        assert result.status_code == 201, result.text
        if committed:
            assert result.json() == lookup.json()["receipt"]
        assert (
            await restarted.post(f"{base}/settlements", json=command, headers=headers)
        ).json() == result.json()
        credits = (
            await restarted.get(f"{base}/participants/{draft['participantId']}/credits")
        ).json()
        assert [o["available"]["amount"] for o in credits] == ["0.00", "25.00"]
        obligations = (
            await restarted.get(f"{base}/participants/{draft['participantId']}/obligations")
        ).json()
        assert obligations[0]["creditApplied"]["amount"] == "25.00"
        assert obligations[0]["remaining"]["amount"] == "15.00"


@pytest.mark.parametrize("same_key", [True, False])
async def test_concurrent_application_cannot_reuse_credit_or_double_settle_debt(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    same_key: bool,
) -> None:
    client, actor, base, draft = advance_client
    await retain_credit(client, actor, base, draft, "50.00")
    debt = await advance(client, actor, base, draft, "100.00", "2026-10-21")
    body = application(draft, [debt["obligation"]["id"]], "30.00")
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    command = {**body, "previewToken": preview["previewToken"]}
    first = {**submission_headers(actor.id), "Finance-Command-Version": "7"}
    second = first if same_key else {**submission_headers(actor.id), "Finance-Command-Version": "7"}
    responses = await asyncio.gather(
        *[client.post(f"{base}/settlements", json=command, headers=h) for h in (first, second)]
    )
    for i, response in enumerate(responses):
        if response.status_code == 503 or response.json().get("code") == "finance_submission_busy":
            responses[i] = await client.post(
                f"{base}/settlements", json=command, headers=(first, second)[i]
            )
    if same_key:
        assert responses[0].status_code == responses[1].status_code == 201
        assert responses[0].json() == responses[1].json()
    else:
        assert sorted(r.status_code for r in responses) == [201, 409]
        rejected = next(r for r in responses if r.status_code == 409)
        assert rejected.json()["code"] == "finance_settlement_preview_stale"
        assert rejected.json()["submissionReceipt"]["commandVersion"] == "7"
    credits = (await client.get(f"{base}/participants/{draft['participantId']}/credits")).json()
    assert credits[0]["available"]["amount"] == "20.00"
