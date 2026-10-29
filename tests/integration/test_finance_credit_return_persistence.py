"""Closed durable return evidence and lossless populated migration boundaries."""

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
from integration.test_finance_credit_applications import application, apply_credit, retain_credit
from integration.test_finance_credit_returns import credit_return, return_credit
from integration.test_finance_nested_submission_persistence import _migrate
from integration.test_finance_settlements import advance, advance_client

pytestmark = pytest.mark.anyio
__all__ = ["advance_client"]


@pytest.mark.parametrize(
    "invalid",
    [
        "kind",
        "account_missing",
        "account_uuid",
        "extra",
        "selection",
        "reason",
        "receipt",
        "zero",
        "negative",
        "precision",
        "number",
        "money_extra",
        "currency",
        "date",
        "operation",
        "legacy",
        "created_type",
        "rejected_code",
    ],
)
async def test_postgresql_rejects_malformed_retained_return_evidence(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    invalid: str,
) -> None:
    client, actor, base, draft = advance_client
    await retain_credit(client, actor, base, draft, "50.00")
    body = credit_return(draft, "25.00")
    plan = (await client.post(f"{base}/settlements/preview", json=body)).json()
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "8"}
    result = await client.post(
        f"{base}/settlements", json={**body, "previewToken": plan["previewToken"]}, headers=headers
    )
    assert result.status_code == 201, result.text
    row = await postgres_session.get(
        FinanceSubmission, (actor.id, UUID(headers["Idempotency-Key"]))
    )
    assert row is not None
    command = deepcopy(row.canonical_command)
    intent: Any = command["body"]
    if invalid == "kind":
        intent["kind"] = "creditApplication"
    elif invalid == "account_missing":
        del intent["payingAccountId"]
    elif invalid == "account_uuid":
        intent["payingAccountId"] = "invalid"
    elif invalid in ("extra", "selection", "reason", "receipt"):
        key, value = {
            "extra": ("excessDisposition", "retainCredit"),
            "selection": ("selectedObligationIds", [draft["participantId"]]),
            "reason": ("reason", "Not forgiveness"),
            "receipt": ("receivingAccountId", draft["fundingAccountId"]),
        }[invalid]
        intent[key] = value
    elif invalid in ("zero", "negative", "precision", "number"):
        intent["amount"]["amount"] = {
            "zero": "0.00",
            "negative": "-1.00",
            "precision": "1.001",
            "number": 1,
        }[invalid]
    elif invalid == "money_extra":
        intent["amount"]["other"] = True
    elif invalid == "currency":
        intent["amount"]["currency"] = "EUR"
    elif invalid == "date":
        intent["transactionDate"] = "2026-02-30"
    elif invalid == "operation":
        command["operation"] = "createFinanceAdvance"
    elif invalid == "legacy":
        command["commandVersion"] = "7"
    elif invalid == "created_type":
        row.terminal_outcome = {
            "kind": "created",
            "resource": {"type": "transaction", "id": str(UUID(int=1))},
        }
    else:
        row.terminal_outcome = {
            "kind": "rejected",
            "problem": {
                "type": "about:blank",
                "title": "Conflict",
                "status": 409,
                "code": "finance_advance_preview_stale",
                "detail": "wrong evidence",
            },
        }
    row.canonical_command = command
    with pytest.raises(IntegrityError):
        await postgres_session.commit()


@pytest.mark.parametrize("state", ["unfinished", "created", "rejected"])
async def test_all_return_submission_states_refuse_destructive_downgrade(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    postgres_engine: AsyncEngine,
    state: str,
) -> None:
    client, actor, base, draft = advance_client
    await retain_credit(client, actor, base, draft, "50.00")
    body = credit_return(draft, "25.00")
    plan = (await client.post(f"{base}/settlements/preview", json=body)).json()
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "8"}
    command = {
        **body,
        "note": None,
        "previewToken": plan["previewToken"] if state == "created" else "0" * 64,
    }
    if state == "unfinished":
        postgres_session.add(
            FinanceSubmission(
                local_user_id=actor.id,
                submission_id=UUID(headers["Idempotency-Key"]),
                command_version="8",
                retention_ledger_id=UUID(base.rsplit("/", 1)[1]),
                canonical_command={
                    "commandVersion": "8",
                    "operation": "createFinanceSettlement",
                    "targetLedgerId": base.rsplit("/", 1)[1],
                    "body": command,
                },
            )
        )
        await postgres_session.commit()
    else:
        result = await client.post(f"{base}/settlements", json=command, headers=headers)
        assert result.status_code == (201 if state == "created" else 409), result.text
    url = f"/api/finance/submissions/{headers['Idempotency-Key']}"
    before = (await client.get(url, headers=headers)).json()
    with pytest.raises(DBAPIError, match="credit return evidence"):
        await _migrate(postgres_engine, revision="20261027_01", downgrade=True)
    assert (await client.get(url, headers=headers)).json() == before
    async with postgres_engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "20261029_01"
        )


async def test_populated_t10_upgrade_preserves_application_origin_history_and_replay(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_engine: AsyncEngine,
) -> None:
    client, actor, base, draft = advance_client
    await retain_credit(client, actor, base, draft, "50.00")
    debt = await advance(client, actor, base, draft, "100.00", "2026-10-21")
    body = application(draft, [debt["obligation"]["id"]], "25.00")
    detail = await apply_credit(client, actor, base, body)
    paths = [
        "accounts",
        "transactions",
        f"advances/{debt['id']}",
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
                    "WHERE command_version = '7'"
                )
            )
        ).one()
    await _migrate(postgres_engine, revision="20261027_01", downgrade=True)
    await _migrate(postgres_engine, revision="head", downgrade=False)
    assert [(await client.get(f"{base}/{p}")).json() for p in paths] == before
    async with postgres_engine.connect() as connection:
        assert (
            await connection.execute(
                text("SELECT * FROM finance_submissions ORDER BY submission_id")
            )
        ).all() == submissions
    assert (await client.get(f"{base}/settlements/{detail['id']}")).json() == detail
    replay = await client.post(
        f"{base}/settlements",
        json=retained.canonical_command["body"],
        headers={
            **submission_headers(actor.id, retained.submission_id),
            "Finance-Command-Version": "7",
        },
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["outcome"]["resource"]["id"] == detail["id"]
    await return_credit(client, actor, base, credit_return(draft, "25.00"))
