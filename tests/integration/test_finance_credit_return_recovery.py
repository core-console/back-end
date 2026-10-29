"""Credit returns recover one complete financial outcome after faults."""

import asyncio
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient, ReadError, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance.credit_return_models import (
    FinanceCreditReturnFragment,
)
from core_console.modules.finance.models import FinanceAccountMovement
from core_console.modules.finance.submission_models import FinanceSubmission
from core_console.modules.users.models import User
from integration.finance_submission_helpers import submission_headers
from integration.test_finance_api import finance_client
from integration.test_finance_credit_applications import application, retain_credit
from integration.test_finance_credit_returns import credit_return, return_credit
from integration.test_finance_settlements import advance, advance_client

pytestmark = pytest.mark.anyio
__all__ = ["advance_client"]


@pytest.mark.parametrize(
    "phase", ["fragment_flush", "before_commit", "admission_ack", "terminal_ack", "response_loss"]
)
async def test_return_rollback_and_restart_recover_all_origin_and_cash_effects(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    client, actor, base, draft = advance_client
    await retain_credit(client, actor, base, draft, "20.00")
    await retain_credit(client, actor, base, draft, "30.00")
    body = credit_return(draft, "25.00")
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    command = {**body, "previewToken": preview["previewToken"]}
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "8"}
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
        written = any(isinstance(row, FinanceCreditReturnFragment) for row in session.new)
        await real_flush(session, objects)
        if written:
            raise RuntimeError("origin consumption and linked transfer flushed before failure")

    handle = ASGITransport.handle_async_request

    async def drop_response(transport: ASGITransport, request: Request) -> Response:
        response = await handle(transport, request)
        assert response.status_code == 201, response.text
        assert (
            await postgres_session.scalar(
                select(func.count()).select_from(FinanceCreditReturnFragment)
            )
            == 2
        )
        binding = await postgres_session.get(
            FinanceSubmission, (actor.id, UUID(headers["Idempotency-Key"]))
        )
        assert binding is not None and binding.terminal_outcome is not None
        assert binding.canonical_command["body"] == {**command, "note": None}
        movements = (
            await postgres_session.scalars(
                select(FinanceAccountMovement.amount).join(
                    FinanceCreditReturnFragment,
                    FinanceCreditReturnFragment.transaction_id
                    == FinanceAccountMovement.transaction_id,
                )
            )
        ).all()
        assert sorted(str(m) for m in movements) == ["-20.00", "-20.00", "-5.00", "-5.00"]
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
        select(func.count()).select_from(FinanceCreditReturnFragment)
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
        assert obligations == []
        accounts = {a["id"]: a for a in (await restarted.get(f"{base}/accounts")).json()}
        assert accounts[str(draft["fundingAccountId"])]["currentBalance"]["amount"] == "125.00"


@pytest.mark.parametrize("same_key", [True, False])
async def test_concurrent_returns_cannot_double_consume_or_pay(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    same_key: bool,
) -> None:
    client, actor, base, draft = advance_client
    await retain_credit(client, actor, base, draft, "50.00")
    body = credit_return(draft, "30.00")
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    command = {**body, "previewToken": preview["previewToken"]}
    first = {**submission_headers(actor.id), "Finance-Command-Version": "8"}
    second = first if same_key else {**submission_headers(actor.id), "Finance-Command-Version": "8"}
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
        assert rejected.json()["submissionReceipt"]["commandVersion"] == "8"
    credits = (await client.get(f"{base}/participants/{draft['participantId']}/credits")).json()
    assert credits[0]["available"]["amount"] == "20.00"


async def test_competing_return_and_application_cannot_consume_the_same_credit(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    await retain_credit(client, actor, base, draft, "50.00")
    debt = await advance(client, actor, base, draft, "100.00", "2026-10-21")
    bodies = [
        credit_return(draft, "30.00"),
        application(draft, [debt["obligation"]["id"]], "30.00"),
    ]
    commands = []
    for body in bodies:
        plan = await client.post(f"{base}/settlements/preview", json=body)
        assert plan.status_code == 200, plan.text
        commands.append({**body, "previewToken": plan.json()["previewToken"]})
    headers = [
        {**submission_headers(actor.id), "Finance-Command-Version": version}
        for version in ("8", "7")
    ]
    responses = await asyncio.gather(
        *[
            client.post(f"{base}/settlements", json=command, headers=header)
            for command, header in zip(commands, headers, strict=True)
        ]
    )
    for i, response in enumerate(responses):
        if response.status_code == 503 or response.json().get("code") == "finance_submission_busy":
            responses[i] = await client.post(
                f"{base}/settlements", json=commands[i], headers=headers[i]
            )
    assert sorted(r.status_code for r in responses) == [201, 409]
    rejected = next(r for r in responses if r.status_code == 409)
    assert rejected.json()["code"] == "finance_settlement_preview_stale"
    credits = (await client.get(f"{base}/participants/{draft['participantId']}/credits")).json()
    assert credits[0]["available"]["amount"] == "20.00"
    await return_credit(client, actor, base, credit_return(draft, "20.00"))
    assert (
        await client.post(f"{base}/settlements/preview", json=credit_return(draft, "1.00"))
    ).status_code == 422
