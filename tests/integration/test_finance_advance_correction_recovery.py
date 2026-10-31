"""One durable Correction outcome across rollback, response loss and races."""

import asyncio
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient, ReadError, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance.advance_correction_models import FinanceAdvanceCorrection
from core_console.modules.finance.models import FinanceAccountMovement
from core_console.modules.finance.submission_models import FinanceSubmission
from core_console.modules.users.models import User
from integration.finance_submission_helpers import submission_headers
from integration.test_finance_advance_corrections import correction
from integration.test_finance_api import finance_client
from integration.test_finance_settlements import advance, advance_client, repayment

pytestmark = pytest.mark.anyio
__all__ = ["advance_client"]


@pytest.mark.parametrize(
    "phase", ["effect_flush", "before_commit", "admission_ack", "terminal_ack", "response_loss"]
)
async def test_correction_recovers_same_complete_outcome_after_restart(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = correction(source)
    preview = (await client.post(f"{base}/advance-corrections/preview", json=body)).json()
    command = {**body, "previewToken": preview["previewToken"]}
    headers = submission_headers(actor.id)
    paths = [
        "accounts",
        "transactions",
        f"advances/{source['id']}",
        f"advances/{source['id']}/corrections",
    ]
    before = [(await client.get(f"{base}/{path}")).json() for path in paths]
    real_commit, real_flush = AsyncSession.commit, AsyncSession.flush
    commits = 0

    async def fail_commit(session: AsyncSession) -> None:
        nonlocal commits
        commits += 1
        if commits == 2 and phase == "before_commit":
            raise ConnectionError("terminal commit did not reach PostgreSQL")
        await real_commit(session)
        if commits == (1 if phase == "admission_ack" else 2):
            raise ConnectionError("commit acknowledgement lost")

    async def fail_flush(session: AsyncSession, objects: Any = None) -> None:
        written = any(isinstance(row, FinanceAdvanceCorrection) for row in session.new)
        await real_flush(session, objects)
        if written:
            raise RuntimeError("complete Correction effects flushed before failure")

    real_handle = ASGITransport.handle_async_request

    async def drop_response(transport: ASGITransport, request: Request) -> Response:
        response = await real_handle(transport, request)
        assert response.status_code == 201, response.text
        # A separate real PostgreSQL connection observes commit before response loss.
        assert (
            await postgres_session.scalar(
                select(func.count()).select_from(FinanceAdvanceCorrection)
            )
            == 1
        )
        row = await postgres_session.get(
            FinanceSubmission, (actor.id, UUID(headers["Idempotency-Key"]))
        )
        assert row is not None and row.terminal_outcome is not None
        assert row.canonical_command["body"] == command
        movements = (await postgres_session.scalars(select(FinanceAccountMovement.amount))).all()
        assert sorted(str(m) for m in movements) == ["-20.00", "-80.00", "80.00"]
        await postgres_session.rollback()
        raise ReadError("committed Correction response lost", request=request)

    with monkeypatch.context() as fault:
        if phase == "response_loss":
            fault.setattr(ASGITransport, "handle_async_request", drop_response)
            with pytest.raises(ReadError):
                await client.post(f"{base}/advance-corrections", json=command, headers=headers)
        else:
            fault.setattr(
                AsyncSession,
                "flush" if phase == "effect_flush" else "commit",
                fail_flush if phase == "effect_flush" else fail_commit,
            )
            failed = await client.post(f"{base}/advance-corrections", json=command, headers=headers)
            assert failed.status_code == 500, failed.text
            assert "submissionReceipt" not in failed.json()
            assert "commandValidationRejection" not in failed.json()
    committed = phase in ("terminal_ack", "response_loss")
    assert await postgres_session.scalar(
        select(func.count()).select_from(FinanceAdvanceCorrection)
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
        result = await restarted.post(f"{base}/advance-corrections", json=command, headers=headers)
        assert result.status_code == 201, result.text
        if committed:
            assert result.json() == lookup.json()["receipt"]
        assert (
            await restarted.post(f"{base}/advance-corrections", json=command, headers=headers)
        ).json() == result.json()
        history = (await restarted.get(f"{base}/advances/{source['id']}/corrections")).json()
        assert len(history) == 1
        assert history[0]["before"] == source
        assert history[0]["after"]["ownerShare"]["amount"] == "20.00"


@pytest.mark.parametrize("same_key", [True, False])
async def test_concurrent_corrections_cannot_duplicate_or_overwrite_unconfirmed_effects(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    same_key: bool,
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = correction(source)
    preview = (await client.post(f"{base}/advance-corrections/preview", json=body)).json()
    command = {**body, "previewToken": preview["previewToken"]}
    first = submission_headers(actor.id)
    headers = [first, first if same_key else submission_headers(actor.id)]
    responses = await asyncio.gather(
        *[client.post(f"{base}/advance-corrections", json=command, headers=h) for h in headers]
    )
    for i, response in enumerate(responses):
        if response.status_code == 503 or response.json().get("code") == "finance_submission_busy":
            responses[i] = await client.post(
                f"{base}/advance-corrections", json=command, headers=headers[i]
            )
    if same_key:
        assert responses[0].status_code == responses[1].status_code == 201
        assert responses[0].json() == responses[1].json()
    else:
        assert sorted(r.status_code for r in responses) == [201, 409]
        assert (
            next(r for r in responses if r.status_code == 409).json()["code"]
            == "finance_advance_correction_preview_stale"
        )
    assert len((await client.get(f"{base}/advances/{source['id']}/corrections")).json()) == 1


async def test_correction_racing_cash_repayment_rejects_the_losing_plan(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    paths = ["advance-corrections", "settlements"]
    bodies = [correction(source), repayment(draft, "10.00")]
    commands = []
    for path, body in zip(paths, bodies, strict=True):
        preview = await client.post(f"{base}/{path}/preview", json=body)
        assert preview.status_code == 200, preview.text
        commands.append({**body, "previewToken": preview.json()["previewToken"]})
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
    assert sorted(r.status_code for r in responses) == [201, 409]
    current = (await client.get(f"{base}/advances/{source['id']}")).json()
    obligations = current.get("obligations", [current.get("obligation")])
    assert obligations[0]["remaining"]["amount"] == (
        "80.00" if responses[0].status_code == 201 else "90.00"
    )
