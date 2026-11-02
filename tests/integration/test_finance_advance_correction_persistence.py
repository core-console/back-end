"""Closed retained Correction evidence and lossless migration boundaries."""

from copy import deepcopy
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core_console.modules.finance.submission_models import FinanceSubmission
from core_console.modules.users.models import User
from integration.finance_submission_helpers import submission_headers
from integration.test_finance_advance_corrections import correction
from integration.test_finance_credit_applications import retain_credit
from integration.test_finance_credit_returns import credit_return, return_credit
from integration.test_finance_nested_submission_persistence import _migrate
from integration.test_finance_settlements import advance, advance_client

pytestmark = pytest.mark.anyio
__all__ = ["advance_client"]


@pytest.mark.parametrize(
    "invalid",
    [
        "reason",
        "advance",
        "unknown",
        "missing",
        "zero",
        "negative",
        "number",
        "scale",
        "currency",
        "date",
        "sum",
        "duplicate",
        "version",
        "operation",
        "created_type",
        "rejected_code",
    ],
)
async def test_postgresql_rejects_malformed_retained_correction_evidence(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    invalid: str,
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = correction(source)
    plan = (await client.post(f"{base}/advance-corrections/preview", json=body)).json()
    headers = submission_headers(actor.id)
    result = await client.post(
        f"{base}/advance-corrections",
        json={**body, "previewToken": plan["previewToken"]},
        headers=headers,
    )
    assert result.status_code == 201, result.text
    row = await postgres_session.get(
        FinanceSubmission, (actor.id, UUID(headers["Idempotency-Key"]))
    )
    assert row is not None
    command = deepcopy(row.canonical_command)
    intent: Any = command["body"]
    if invalid == "reason":
        intent["reason"] = " "
    elif invalid == "advance":
        intent["advanceId"] = "invalid"
    elif invalid == "unknown":
        intent["releaseAllocations"] = []
    elif invalid == "missing":
        del intent["ownerShare"]
    elif invalid in ("zero", "negative", "number", "scale"):
        intent["amount"]["amount"] = {
            "zero": "0.00",
            "negative": "-1.00",
            "number": 100,
            "scale": "100.000",
        }[invalid]
    elif invalid == "currency":
        intent["ownerShare"]["currency"] = "USD"
    elif invalid == "date":
        intent["transactionDate"] = "2026-02-30"
    elif invalid == "sum":
        intent["ownerShare"]["amount"] = "19.00"
    elif invalid == "duplicate":
        intent["participantShares"] *= 2
    elif invalid == "version":
        command["commandVersion"] = row.command_version = "2"
    elif invalid == "operation":
        command["operation"] = "createFinanceAdvance"
    elif invalid == "created_type":
        row.terminal_outcome = {
            "kind": "created",
            "resource": {"type": "advance", "id": source["id"]},
        }
    else:
        row.terminal_outcome = {
            "kind": "rejected",
            "problem": {
                "type": "about:blank",
                "title": "Conflict",
                "status": 409,
                "code": "finance_advance_preview_stale",
                "detail": "Wrong command evidence",
            },
        }
    row.canonical_command = command
    with pytest.raises(IntegrityError):
        await postgres_session.commit()


@pytest.mark.parametrize("state", ["unfinished", "created", "rejected"])
async def test_all_correction_states_refuse_destructive_downgrade(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    postgres_engine: AsyncEngine,
    state: str,
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = correction(source)
    plan = (await client.post(f"{base}/advance-corrections/preview", json=body)).json()
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
                    "operation": "createFinanceAdvanceCorrection",
                    "targetLedgerId": base.rsplit("/", 1)[1],
                    "body": command,
                },
            )
        )
        await postgres_session.commit()
    else:
        result = await client.post(f"{base}/advance-corrections", json=command, headers=headers)
        assert result.status_code == (201 if state == "created" else 409), result.text
    url = f"/api/finance/submissions/{headers['Idempotency-Key']}"
    before = (await client.get(url, headers=headers)).json()
    with pytest.raises(DBAPIError, match="Advance correction evidence"):
        await _migrate(postgres_engine, revision="20261029_01", downgrade=True)
    assert (await client.get(url, headers=headers)).json() == before
    async with postgres_engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "20261102_01"
        )


async def test_populated_t11_migration_preserves_sources_credit_returns_and_submission_replay(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_engine: AsyncEngine,
) -> None:
    client, actor, base, draft = advance_client
    origin = await retain_credit(client, actor, base, draft, "50.00")
    returned = await return_credit(client, actor, base, credit_return(draft, "20.00"))
    origin = (await client.get(f"{base}/settlements/{origin['id']}")).json()
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    paths = [
        "accounts",
        "transactions",
        f"advances/{source['id']}",
        f"participants/{draft['participantId']}/credits",
        f"participants/{draft['participantId']}/settlements",
    ]
    before = [(await client.get(f"{base}/{p}")).json() for p in paths]
    async with postgres_engine.connect() as connection:
        submissions = (
            await connection.execute(
                text("SELECT * FROM finance_submissions ORDER BY submission_id")
            )
        ).all()
        retained = (
            await connection.execute(
                text(
                    "SELECT submission_id, canonical_command FROM finance_submissions "
                    "WHERE command_version = '8'"
                )
            )
        ).one()
    await _migrate(postgres_engine, revision="20261029_01", downgrade=True)
    await _migrate(postgres_engine, revision="head", downgrade=False)
    assert [(await client.get(f"{base}/{p}")).json() for p in paths] == before
    async with postgres_engine.connect() as connection:
        assert (
            await connection.execute(
                text("SELECT * FROM finance_submissions ORDER BY submission_id")
            )
        ).all() == submissions
    for detail in (origin, returned):
        assert (await client.get(f"{base}/settlements/{detail['id']}")).json() == detail
    replay = await client.post(
        f"{base}/settlements",
        json=retained.canonical_command["body"],
        headers={
            **submission_headers(actor.id, retained.submission_id),
            "Finance-Command-Version": "8",
        },
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["outcome"]["resource"]["id"] == returned["id"]
