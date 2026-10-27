"""PostgreSQL guards and populated migration retention for cash repayments."""

from copy import deepcopy
from typing import Any, cast
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
from integration.test_finance_nested_submission_persistence import _migrate
from integration.test_finance_settlements import advance, repayment

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "invalid",
    [
        "version",
        "operation",
        "extra",
        "missing",
        "note",
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
async def test_postgresql_rejects_invalid_retained_settlement_evidence(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    invalid: str,
) -> None:
    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = repayment(draft)
    preview = await client.post(f"{base}/settlements/preview", json=body)
    headers = submission_headers(actor.id)
    result = await client.post(
        f"{base}/settlements",
        json={**body, "previewToken": preview.json()["previewToken"]},
        headers=headers,
    )
    assert result.status_code == 201, result.text
    row = await postgres_session.get(
        FinanceSubmission, (actor.id, UUID(headers["Idempotency-Key"]))
    )
    assert row is not None
    command = deepcopy(row.canonical_command)
    command_body: Any = command["body"]
    outcome = deepcopy(row.terminal_outcome)
    if invalid == "version":
        command["commandVersion"] = "2"
    elif invalid == "operation":
        command["operation"] = "createFinanceAdvance"
    elif invalid == "extra":
        command_body["selectedObligationIds"] = []
    elif invalid == "missing":
        del command_body["note"]
    elif invalid == "note":
        command_body["note"] = " untrimmed "
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
        cast(dict[str, Any], outcome["resource"])["type"] = "advance"
    else:
        outcome = {
            "kind": "rejected",
            "problem": {
                "type": "about:blank",
                "title": "Conflict",
                "status": 409,
                "code": "finance_advance_preview_stale",
                "detail": "wrong command evidence",
            },
        }
    row.canonical_command = command
    row.terminal_outcome = outcome
    with pytest.raises(IntegrityError):
        await postgres_session.commit()


@pytest.mark.parametrize("state", ["unfinished", "created", "rejected"])
async def test_downgrade_preserves_every_retained_settlement_state(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    postgres_engine: AsyncEngine,
    state: str,
) -> None:
    client, actor, base, draft = advance_client
    await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = repayment(draft)
    preview = await client.post(f"{base}/settlements/preview", json=body)
    command = {
        **body,
        "previewToken": preview.json()["previewToken"] if state == "created" else "0" * 64,
    }
    key = uuid4()
    if state == "unfinished":
        postgres_session.add(
            FinanceSubmission(
                local_user_id=actor.id,
                submission_id=key,
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
    else:
        result = await client.post(
            f"{base}/settlements", json=command, headers=submission_headers(actor.id, key)
        )
        assert result.status_code == (201 if state == "created" else 409), result.text
    headers = submission_headers(actor.id, key)
    before = (await client.get(f"/api/finance/submissions/{key}", headers=headers)).json()
    with pytest.raises(DBAPIError, match="retained Settlement evidence"):
        await _migrate(postgres_engine, revision="20261018_02", downgrade=True)
    async with postgres_engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "20261027_01"
        )
    assert (await client.get(f"/api/finance/submissions/{key}", headers=headers)).json() == before


async def test_populated_upgrade_preserves_advance_sources_and_receipts(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_engine: AsyncEngine,
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    positions = (await client.get(f"{base}/accounts")).json()
    await _migrate(postgres_engine, revision="20261018_02", downgrade=True)
    await _migrate(postgres_engine, revision="head", downgrade=False)
    assert (await client.get(f"{base}/advances/{source['id']}")).json() == source
    assert (await client.get(f"{base}/accounts")).json() == positions
    preview = await client.post(f"{base}/settlements/preview", json=repayment(draft))
    assert preview.status_code == 200, preview.text
