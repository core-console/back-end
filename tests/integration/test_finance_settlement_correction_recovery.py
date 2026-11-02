"""One durable receipt Correction outcome across faults, restarts and contention."""

import asyncio
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient, ReadError, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from core_console.modules.finance.models import FinanceAccountMovement
from core_console.modules.finance.settlement_correction_models import (
    FinanceSettlementCorrection,
    FinanceSettlementCorrectionTransaction,
)
from core_console.modules.finance.settlement_models import (
    FinanceCreditOrigin,
    FinanceSettlementAllocation,
    FinanceSettlementIncome,
)
from core_console.modules.finance.submission_models import FinanceSubmission
from core_console.modules.users.models import User
from integration.finance_submission_helpers import submission_headers
from integration.test_finance_api import finance_client
from integration.test_finance_credit_applications import application, retain_credit
from integration.test_finance_settlement_corrections import correction, money
from integration.test_finance_settlements import advance, advance_client, confirm, repayment

pytestmark = pytest.mark.anyio
__all__ = ["advance_client"]


@pytest.mark.parametrize("same_key", [True, False])
async def test_racing_corrections_converge_or_require_new_confirmation(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    same_key: bool,
) -> None:
    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "100.00", "2026-10-20")
    source = await confirm(client, actor, base, repayment(draft, "80.00"))
    body = correction(source)
    preview = await client.post(f"{base}/settlement-corrections/preview", json=body)
    command = {**body, "previewToken": preview.json()["previewToken"]}
    first = submission_headers(actor.id)
    headers = [first, first if same_key else submission_headers(actor.id)]
    results = await asyncio.gather(
        *[client.post(f"{base}/settlement-corrections", json=command, headers=h) for h in headers]
    )
    for i, result in enumerate(results):
        if result.status_code == 503 or result.json().get("code") == "finance_submission_busy":
            results[i] = await client.post(
                f"{base}/settlement-corrections", json=command, headers=headers[i]
            )
    if same_key:
        assert results[0].status_code == results[1].status_code == 201
        assert results[0].json() == results[1].json()
    else:
        assert sorted(r.status_code for r in results) == [201, 409], [r.text for r in results]
        assert (
            next(r for r in results if r.status_code == 409).json()["code"]
            == "finance_settlement_correction_preview_stale"
        )
    assert len((await client.get(f"{base}/settlements/{source['id']}/corrections")).json()) == 1


@pytest.mark.parametrize(
    "phase", ["effect_flush", "before_commit", "admission_ack", "terminal_ack", "response_loss"]
)
@pytest.mark.parametrize(
    "target, disposition",
    [
        ("70.00", "none"),
        ("110.00", "retainCredit"),
        ("110.00", "acceptIncome"),
    ],
)
async def test_correction_recovers_same_complete_outcome_after_restart(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    target: str,
    disposition: str,
) -> None:
    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "100.00", "2026-10-20")
    source = await confirm(client, actor, base, repayment(draft, "80.00"))
    body = correction(source, target, disposition)
    preview = (await client.post(f"{base}/settlement-corrections/preview", json=body)).json()
    command = {**body, "previewToken": preview["previewToken"]}
    headers = submission_headers(actor.id)
    paths = [
        "accounts",
        "transactions",
        f"settlements/{source['id']}",
        f"settlements/{source['id']}/corrections",
    ]
    before = [(await client.get(f"{base}/{path}")).json() for path in paths]
    real_commit, real_flush = AsyncSession.commit, Session.flush
    commits = 0

    async def fail_commit(session: AsyncSession) -> None:
        nonlocal commits
        commits += 1
        if commits == 2 and phase == "before_commit":
            raise ConnectionError("terminal commit did not reach PostgreSQL")
        await real_commit(session)
        if commits == (1 if phase == "admission_ack" else 2):
            raise ConnectionError("commit acknowledgement lost")

    def fail_flush(session: Session, objects: Any = None) -> None:
        written = any(
            isinstance(row, FinanceSettlementCorrectionTransaction) for row in session.new
        )
        real_flush(session, objects)
        if written:
            raise RuntimeError("complete Correction effects flushed before failure")

    real_handle = ASGITransport.handle_async_request

    async def drop_response(transport: ASGITransport, request: Request) -> Response:
        response = await real_handle(transport, request)
        assert response.status_code == 201, response.text
        # A separate real PostgreSQL connection observes commit before response loss.
        assert (
            await postgres_session.scalar(
                select(func.count()).select_from(FinanceSettlementCorrection)
            )
            == 1
        )
        row = await postgres_session.get(
            FinanceSubmission, (actor.id, UUID(headers["Idempotency-Key"]))
        )
        assert row is not None and row.terminal_outcome is not None
        assert row.canonical_command["body"] == command
        # Independently observe the complete committed cash and resolution effects.
        assert await postgres_session.scalar(
            select(func.sum(FinanceAccountMovement.amount)).where(
                FinanceAccountMovement.account_id == UUID(str(draft["fundingAccountId"]))
            )
        ) == (Decimal("-30.00") if target == "70.00" else Decimal("10.00"))
        assert await postgres_session.scalar(
            select(FinanceSettlementAllocation.effective_cash_paid).where(
                FinanceSettlementAllocation.settlement_id == UUID(source["id"])
            )
        ) == (Decimal("70.00") if target == "70.00" else Decimal("100.00"))
        assert await postgres_session.scalar(
            select(FinanceCreditOrigin.amount).where(
                FinanceCreditOrigin.settlement_id == UUID(source["id"])
            )
        ) == (Decimal("10.00") if disposition == "retainCredit" else None)
        assert await postgres_session.scalar(
            select(FinanceSettlementIncome.amount).where(
                FinanceSettlementIncome.settlement_id == UUID(source["id"])
            )
        ) == (Decimal("10.00") if disposition == "acceptIncome" else None)
        await postgres_session.rollback()
        raise ReadError("committed Correction response lost", request=request)

    with monkeypatch.context() as fault:
        if phase == "response_loss":
            fault.setattr(ASGITransport, "handle_async_request", drop_response)
            with pytest.raises(ReadError):
                await client.post(f"{base}/settlement-corrections", json=command, headers=headers)
        else:
            fault.setattr(
                Session if phase == "effect_flush" else AsyncSession,
                "flush" if phase == "effect_flush" else "commit",
                fail_flush if phase == "effect_flush" else fail_commit,
            )
            failed = await client.post(
                f"{base}/settlement-corrections", json=command, headers=headers
            )
            assert failed.status_code == 500, failed.text
            assert "submissionReceipt" not in failed.json()
            assert "commandValidationRejection" not in failed.json()
    committed = phase in ("terminal_ack", "response_loss")
    assert await postgres_session.scalar(
        select(func.count()).select_from(FinanceSettlementCorrection)
    ) == (1 if committed else 0)
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
        result = await restarted.post(
            f"{base}/settlement-corrections", json=command, headers=headers
        )
        assert result.status_code == 201, result.text
        if committed:
            assert result.json() == lookup.json()["receipt"]
        assert (
            await restarted.post(f"{base}/settlement-corrections", json=command, headers=headers)
        ).json() == result.json()
        history = (await restarted.get(f"{base}/settlements/{source['id']}/corrections")).json()
        assert len(history) == 1
        assert history[0]["before"]["amount"] == money("80.00")
        assert history[0]["after"]["amount"] == money(target)


async def test_correction_racing_repayment_rejects_the_losing_complete_plan(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "100.00", "2026-10-20")
    source = await confirm(client, actor, base, repayment(draft, "80.00"))
    paths = ["settlement-corrections", "settlements"]
    bodies = [correction(source), repayment(draft, "10.00")]
    commands = []
    for path, body in zip(paths, bodies, strict=True):
        plan = await client.post(f"{base}/{path}/preview", json=body)
        assert plan.status_code == 200, plan.text
        commands.append({**body, "previewToken": plan.json()["previewToken"]})
    headers = [submission_headers(actor.id) for _ in paths]
    responses = await asyncio.gather(
        *[
            client.post(f"{base}/{path}", json=body, headers=h)
            for path, body, h in zip(paths, commands, headers, strict=True)
        ]
    )
    for i, response in enumerate(responses):
        if response.status_code == 503 or response.json().get("code") == "finance_submission_busy":
            responses[i] = await client.post(
                f"{base}/{paths[i]}", json=commands[i], headers=headers[i]
            )
    assert sorted(r.status_code for r in responses) == [201, 409], [r.text for r in responses]
    debts = (await client.get(f"{base}/participants/{draft['participantId']}/obligations")).json()
    assert debts[0]["remaining"] == money("30.00" if responses[0].status_code == 201 else "10.00")


async def test_correction_racing_credit_application_never_loses_consumed_credit(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "100.00", "2026-10-20")
    source = await retain_credit(client, actor, base, draft, "110.00")
    debt = await advance(client, actor, base, draft, "20.00", "2026-10-21")
    paths = ["settlement-corrections", "settlements"]
    bodies = [
        correction(source, "105.00", "retainCredit"),
        application(draft, [debt["obligation"]["id"]], "5.00"),
    ]
    commands = []
    for path, body in zip(paths, bodies, strict=True):
        plan = await client.post(f"{base}/{path}/preview", json=body)
        assert plan.status_code == 200, plan.text
        commands.append({**body, "previewToken": plan.json()["previewToken"]})
    headers = [
        submission_headers(actor.id),
        {**submission_headers(actor.id), "Finance-Command-Version": "7"},
    ]
    responses = await asyncio.gather(
        *[
            client.post(f"{base}/{path}", json=body, headers=h)
            for path, body, h in zip(paths, commands, headers, strict=True)
        ]
    )
    for i, response in enumerate(responses):
        if response.status_code == 503 or response.json().get("code") == "finance_submission_busy":
            responses[i] = await client.post(
                f"{base}/{paths[i]}", json=commands[i], headers=headers[i]
            )
    assert sorted(r.status_code for r in responses) == [201, 409], [r.text for r in responses]
    credits = (await client.get(f"{base}/participants/{draft['participantId']}/credits")).json()
    assert credits[0]["available"] == money("5.00")
    current = (await client.get(f"{base}/advances/{debt['id']}")).json()["obligation"]
    assert current["remaining"] == money("20.00" if responses[0].status_code == 201 else "15.00")
