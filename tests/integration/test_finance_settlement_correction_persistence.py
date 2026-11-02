"""Closed durable evidence, retained allocation order and lossless migration gates."""

from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core_console.modules.finance.models import FinanceAccountMovement, FinanceTransaction
from core_console.modules.finance.settlement_models import FinanceSettlementAllocation
from core_console.modules.finance.submission_models import FinanceSubmission
from core_console.modules.users.models import User
from integration.finance_submission_helpers import submission_headers
from integration.test_finance_credit_applications import retain_credit
from integration.test_finance_nested_submission_persistence import _migrate
from integration.test_finance_settlement_corrections import correct, correction, money
from integration.test_finance_settlements import advance, advance_client, confirm, repayment

pytestmark = pytest.mark.anyio
__all__ = ["advance_client"]


@pytest.mark.parametrize(
    "invalid",
    [
        "reason",
        "settlement",
        "unknown",
        "missing",
        "zero",
        "negative",
        "number",
        "scale",
        "currency",
        "disposition",
        "category",
        "version",
        "operation",
        "created_type",
        "rejected_code",
    ],
)
async def test_postgresql_rejects_malformed_retained_receipt_correction_evidence(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    invalid: str,
) -> None:
    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "100.00", "2026-10-20")
    source = await confirm(client, actor, base, repayment(draft, "80.00"))
    body = correction(source)
    plan = (await client.post(f"{base}/settlement-corrections/preview", json=body)).json()
    headers = submission_headers(actor.id)
    response = await client.post(
        f"{base}/settlement-corrections",
        json={**body, "previewToken": plan["previewToken"]},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    row = await postgres_session.get(
        FinanceSubmission, (actor.id, UUID(headers["Idempotency-Key"]))
    )
    assert row is not None
    command = deepcopy(row.canonical_command)
    intent: Any = command["body"]
    if invalid == "reason":
        intent["reason"] = " "
    elif invalid == "settlement":
        intent["settlementId"] = "invalid"
    elif invalid == "unknown":
        intent["releaseAllocations"] = []
    elif invalid == "missing":
        del intent["categoryId"]
    elif invalid in ("zero", "negative", "number", "scale"):
        intent["amount"]["amount"] = {
            "zero": "0.00",
            "negative": "-1.00",
            "number": 70,
            "scale": "70.000",
        }[invalid]
    elif invalid == "currency":
        intent["amount"]["currency"] = "EUR"
    elif invalid == "disposition":
        intent["excessDisposition"] = "automatic"
    elif invalid == "category":
        intent["categoryId"] = str(uuid4())
    elif invalid == "version":
        command["commandVersion"] = row.command_version = "2"
    elif invalid == "operation":
        command["operation"] = "createFinanceSettlement"
    elif invalid == "created_type":
        row.terminal_outcome = {
            "kind": "created",
            "resource": {"type": "settlement", "id": source["id"]},
        }
    else:
        row.terminal_outcome = {
            "kind": "rejected",
            "problem": {
                "type": "about:blank",
                "title": "Conflict",
                "status": 409,
                "code": "finance_settlement_preview_stale",
                "detail": "Wrong operation evidence",
            },
        }
    row.canonical_command = command
    with pytest.raises(IntegrityError):
        await postgres_session.commit()


@pytest.mark.parametrize("state", ["unfinished", "created", "rejected"])
async def test_all_receipt_correction_states_refuse_destructive_downgrade(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    postgres_engine: AsyncEngine,
    state: str,
) -> None:
    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "100.00", "2026-10-20")
    source = await confirm(client, actor, base, repayment(draft, "80.00"))
    body = correction(source)
    plan = (await client.post(f"{base}/settlement-corrections/preview", json=body)).json()
    headers = submission_headers(actor.id)
    command = {**body, "previewToken": plan["previewToken"] if state == "created" else "0" * 64}
    if state == "unfinished":
        postgres_session.add(
            FinanceSubmission(
                local_user_id=actor.id,
                submission_id=UUID(headers["Idempotency-Key"]),
                command_version="1",
                retention_ledger_id=UUID(base.rsplit("/", 1)[1]),
                canonical_command={
                    "commandVersion": "1",
                    "operation": "createFinanceSettlementCorrection",
                    "targetLedgerId": base.rsplit("/", 1)[1],
                    "body": command,
                },
            )
        )
        await postgres_session.commit()
    else:
        result = await client.post(f"{base}/settlement-corrections", json=command, headers=headers)
        assert result.status_code == (201 if state == "created" else 409), result.text
    path = f"/api/finance/submissions/{headers['Idempotency-Key']}"
    before = (await client.get(path, headers=headers)).json()
    with pytest.raises(DBAPIError, match="Settlement correction evidence"):
        await _migrate(postgres_engine, revision="20261031_01", downgrade=True)
    assert (await client.get(path, headers=headers)).json() == before
    async with postgres_engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "20261102_01"
        )


async def test_populated_t12_migration_preserves_receipts_allocations_credit_and_replay(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_engine: AsyncEngine,
) -> None:
    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "100.00", "2026-10-20")
    source = await retain_credit(client, actor, base, draft, "110.00")
    paths = [
        "accounts",
        "transactions",
        f"settlements/{source['id']}",
        f"participants/{draft['participantId']}/obligations",
        f"participants/{draft['participantId']}/credits",
    ]
    before = [(await client.get(f"{base}/{p}")).json() for p in paths]
    async with postgres_engine.connect() as connection:
        rows = (
            await connection.execute(
                text("SELECT * FROM finance_submissions ORDER BY submission_id")
            )
        ).all()
        command = next(
            r for r in rows if r.canonical_command["operation"] == "createFinanceSettlement"
        )
    await _migrate(postgres_engine, revision="20261031_01", downgrade=True)
    await _migrate(postgres_engine, revision="head", downgrade=False)
    assert [(await client.get(f"{base}/{p}")).json() for p in paths] == before
    async with postgres_engine.connect() as connection:
        assert (
            await connection.execute(
                text("SELECT * FROM finance_submissions ORDER BY submission_id")
            )
        ).all() == rows
    result = await client.post(
        f"{base}/settlements",
        json=command.canonical_command["body"],
        headers={
            **submission_headers(actor.id, command.submission_id),
            "Finance-Command-Version": "2",
        },
    )
    assert result.status_code == 201, result.text
    assert result.json()["outcome"]["resource"]["id"] == source["id"]


async def test_original_order_survives_linked_transaction_replacement_and_effective_release(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
) -> None:
    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "40.00", "2026-10-20")
    await advance(client, actor, base, draft, "60.00", "2026-10-21")
    source = await confirm(client, actor, base, repayment(draft, "100.00"))
    rows = list(
        await postgres_session.scalars(
            select(FinanceSettlementAllocation)
            .where(FinanceSettlementAllocation.settlement_id == UUID(source["id"]))
            .order_by(FinanceSettlementAllocation.position)
        )
    )
    original = [(r.position, r.obligation_id, r.cash_paid, r.remaining_after) for r in rows]
    # Simulate coordinated replacement of linked Finance Transactions with identical facts.
    # Deliberately reverse UUID order; immutable Allocation positions must remain authoritative.
    for allocation, new_id in zip(rows, sorted([uuid4(), uuid4()], reverse=True), strict=True):
        old_id = allocation.transaction_id
        old = await postgres_session.get(FinanceTransaction, old_id)
        assert old is not None
        movements = list(
            await postgres_session.scalars(
                select(FinanceAccountMovement).where(
                    FinanceAccountMovement.transaction_id == old_id
                )
            )
        )
        postgres_session.add(
            FinanceTransaction(
                id=new_id,
                ledger_id=old.ledger_id,
                kind=old.kind,
                transaction_date=old.transaction_date,
                note=old.note,
            )
        )
        await postgres_session.flush()
        for m in movements:
            postgres_session.add(
                FinanceAccountMovement(
                    transaction_id=new_id,
                    ledger_id=m.ledger_id,
                    account_id=m.account_id,
                    amount=m.amount,
                    currency=m.currency,
                    role=m.role,
                )
            )
        allocation.transaction_id = new_id
        await postgres_session.flush()
        await postgres_session.execute(
            delete(FinanceAccountMovement).where(FinanceAccountMovement.transaction_id == old_id)
        )
        await postgres_session.delete(old)
    await postgres_session.commit()
    detail = await correct(client, actor, base, correction(source))
    assert [a["cashPaid"] for a in detail["after"]["allocations"]] == [
        money("40.00"),
        money("30.00"),
    ]
    for row in rows:
        await postgres_session.refresh(row)
    assert [(r.position, r.obligation_id, r.cash_paid, r.remaining_after) for r in rows] == original
