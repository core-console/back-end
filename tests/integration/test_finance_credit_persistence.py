"""Retained credit command grammar and lossless populated migration boundaries."""

from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core_console.modules.finance.submission_models import FinanceSubmission
from core_console.modules.users.models import User
from integration.finance_submission_helpers import submission_headers
from integration.test_finance_advances import advance_client as advance_client
from integration.test_finance_credits import retained
from integration.test_finance_nested_submission_persistence import _migrate
from integration.test_finance_settlements import advance, repayment

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "invalid",
    [
        "version",
        "nonstring_version",
        "missing_disposition",
        "income",
        "credit_use",
        "missing_note",
        "date",
        "participant",
        "account",
        "token",
        "float",
        "zero",
        "negative",
        "precision",
        "currency",
        "money_extra",
        "outcome_type",
        "outcome_code",
    ],
)
async def test_postgresql_rejects_invalid_retained_credit_command_and_outcome(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    invalid: str,
) -> None:
    client, actor, base, draft = advance_client
    body = retained(draft, "50.00")
    preview = await client.post(f"{base}/settlements/preview", json=body)
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "2"}
    response = await client.post(
        f"{base}/settlements",
        json={**body, "previewToken": preview.json()["previewToken"]},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    row = await postgres_session.get(
        FinanceSubmission, (actor.id, UUID(headers["Idempotency-Key"]))
    )
    assert row is not None
    command = deepcopy(row.canonical_command)
    command_body: Any = command["body"]
    outcome = deepcopy(row.terminal_outcome)
    if invalid == "version":
        command["commandVersion"] = "1"
    elif invalid == "nonstring_version":
        command["commandVersion"] = 2
    elif invalid == "missing_disposition":
        del command_body["excessDisposition"]
    elif invalid == "income":
        command_body["excessDisposition"] = "acceptIncome"
    elif invalid == "credit_use":
        command_body["useCredit"] = True
    elif invalid == "missing_note":
        del command_body["note"]
    elif invalid == "date":
        command_body["transactionDate"] = "2026-02-30"
    elif invalid in ("participant", "account", "token"):
        command_body[
            {
                "participant": "participantId",
                "account": "receivingAccountId",
                "token": "previewToken",
            }[invalid]
        ] = "bad"
    elif invalid in ("float", "zero", "negative", "precision"):
        command_body["amount"]["amount"] = {
            "float": 50.0,
            "zero": "0.00",
            "negative": "-1.00",
            "precision": "50.000",
        }[invalid]
    elif invalid == "currency":
        command_body["amount"]["currency"] = "EUR"
    elif invalid == "money_extra":
        command_body["amount"]["extra"] = True
    elif invalid == "outcome_type":
        assert outcome is not None
        outcome["resource"] = {"type": "advance", "id": str(uuid4())}
    else:
        outcome = {
            "kind": "rejected",
            "problem": {
                "type": "about:blank",
                "title": "Conflict",
                "status": 409,
                "code": "finance_advance_preview_stale",
                "detail": "wrong operation",
            },
        }
    row.canonical_command, row.terminal_outcome = command, outcome
    with pytest.raises(IntegrityError):
        await postgres_session.commit()


@pytest.mark.parametrize("state", ["unfinished", "created", "rejected"])
async def test_downgrade_refuses_every_retained_v2_credit_binding(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    postgres_engine: AsyncEngine,
    state: str,
) -> None:
    client, actor, base, draft = advance_client
    body = retained(draft, "50.00")
    preview = await client.post(f"{base}/settlements/preview", json=body)
    command = {
        **body,
        "note": body["note"],
        "previewToken": preview.json()["previewToken"] if state == "created" else "0" * 64,
    }
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "2"}
    if state == "unfinished":
        postgres_session.add(
            FinanceSubmission(
                local_user_id=actor.id,
                submission_id=UUID(headers["Idempotency-Key"]),
                command_version="2",
                retention_ledger_id=UUID(base.rsplit("/", 1)[1]),
                canonical_command={
                    "commandVersion": "2",
                    "operation": "createFinanceSettlement",
                    "targetLedgerId": base.rsplit("/", 1)[1],
                    "body": command,
                },
            )
        )
        await postgres_session.commit()
    else:
        response = await client.post(f"{base}/settlements", json=command, headers=headers)
        assert response.status_code == (201 if state == "created" else 409), response.text
    before = (
        await client.get(f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers)
    ).json()
    with pytest.raises(DBAPIError, match="retained credit evidence"):
        await _migrate(postgres_engine, revision="20261019_01", downgrade=True)
    async with postgres_engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "20261029_01"
        )
    assert (
        await client.get(f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers)
    ).json() == before


async def test_populated_upgrade_preserves_v1_preview_binding_sources_and_recovery(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    postgres_engine: AsyncEngine,
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = repayment(draft)
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    command = {**body, "previewToken": preview["previewToken"]}
    headers = submission_headers(actor.id)
    postgres_session.add(
        FinanceSubmission(
            local_user_id=actor.id,
            submission_id=UUID(headers["Idempotency-Key"]),
            command_version="1",
            retention_ledger_id=UUID(base.rsplit("/", 1)[1]),
            canonical_command={
                "commandVersion": "1",
                "operation": "createFinanceSettlement",
                "targetLedgerId": base.rsplit("/", 1)[1],
                "body": command,
            },
        )
    )
    await postgres_session.commit()
    before = (
        await client.get(f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers)
    ).json()
    positions = (await client.get(f"{base}/accounts")).json()
    await _migrate(postgres_engine, revision="20261019_01", downgrade=True)
    await _migrate(postgres_engine, revision="head", downgrade=False)
    assert (await client.get(f"{base}/advances/{source['id']}")).json() == source
    assert (await client.get(f"{base}/accounts")).json() == positions
    assert (await client.post(f"{base}/settlements/preview", json=body)).json() == preview
    assert (
        await client.get(f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers)
    ).json() == before
    result = await client.post(f"{base}/settlements", json=command, headers=headers)
    assert result.status_code == 201, result.text
    assert result.json()["commandVersion"] == "1"
    assert (
        await client.post(f"{base}/settlements", json=command, headers=headers)
    ).json() == result.json()
