"""Real PostgreSQL closed Income grammar and lossless migration boundaries."""

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
from integration.test_finance_credits import confirm_credit, retained
from integration.test_finance_nested_submission_persistence import _migrate
from integration.test_finance_settlements import advance
from integration.test_finance_surplus import accepted

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "invalid",
    [
        "version",
        "operation",
        "disposition",
        "missing_category",
        "extra",
        "missing_note",
        "date",
        "token",
        "float",
        "zero",
        "negative",
        "precision",
        "currency",
        "money_extra",
        "split",
        "category",
        "too_large",
        "outcome",
    ],
)
async def test_postgresql_rejects_malformed_income_commands_and_outcomes(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    invalid: str,
) -> None:
    client, actor, base, draft = advance_client
    body = accepted(draft, "2.00")
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "3"}
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    response = await client.post(
        f"{base}/settlements",
        json={**body, "previewToken": preview["previewToken"]},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    row = await postgres_session.get(
        FinanceSubmission, (actor.id, UUID(headers["Idempotency-Key"]))
    )
    assert row is not None
    command = deepcopy(row.canonical_command)
    intent: Any = command["body"]
    if invalid == "version":
        command["commandVersion"] = "2"
    elif invalid == "operation":
        command["operation"] = "createFinanceAdvance"
    elif invalid == "disposition":
        intent["excessDisposition"] = "retainCredit"
    elif invalid == "missing_category":
        del intent["categoryAllocations"][0]["categoryId"]
    elif invalid == "extra":
        intent["useCredit"] = True
    elif invalid == "missing_note":
        del intent["note"]
    elif invalid == "date":
        intent["transactionDate"] = "2026-02-30"
    elif invalid == "token":
        intent["previewToken"] = "invalid"
    elif invalid in ("float", "zero", "negative", "precision", "too_large"):
        intent["categoryAllocations"][0]["amount"]["amount"] = {
            "float": 2.0,
            "zero": "0.00",
            "negative": "-1.00",
            "precision": "2.000",
            "too_large": "3.00",
        }[invalid]
    elif invalid == "currency":
        intent["categoryAllocations"][0]["amount"]["currency"] = "USD"
    elif invalid == "money_extra":
        intent["categoryAllocations"][0]["amount"]["extra"] = True
    elif invalid == "split":
        intent["categoryAllocations"] *= 2
    elif invalid == "category":
        intent["categoryAllocations"][0]["categoryId"] = "bad"
    else:
        row.terminal_outcome = {
            "kind": "created",
            "resource": {"type": "transaction", "id": str(uuid4())},
        }
    row.canonical_command = command
    with pytest.raises(IntegrityError):
        await postgres_session.commit()


@pytest.mark.parametrize("state", ["unfinished", "created", "rejected"])
async def test_downgrade_refuses_any_bound_income_command_without_losing_evidence(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    postgres_engine: AsyncEngine,
    state: str,
) -> None:
    client, actor, base, draft = advance_client
    body = accepted(draft, "2.00")
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    command = {
        **body,
        "categoryAllocations": [
            {"amount": {"amount": "2.00", "currency": "CNY"}, "categoryId": None}
        ],
        "previewToken": preview["previewToken"] if state == "created" else "0" * 64,
    }
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "3"}
    if state == "unfinished":
        postgres_session.add(
            FinanceSubmission(
                local_user_id=actor.id,
                submission_id=UUID(headers["Idempotency-Key"]),
                command_version="3",
                retention_ledger_id=UUID(base.rsplit("/", 1)[1]),
                canonical_command={
                    "commandVersion": "3",
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
    with pytest.raises(DBAPIError, match="accepted surplus evidence"):
        await _migrate(postgres_engine, revision="20261022_01", downgrade=True)
    async with postgres_engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "20261023_01"
        )
    assert (
        await client.get(f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers)
    ).json() == before


async def test_populated_upgrade_preserves_retained_credit_preview_and_v2_recovery(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    postgres_engine: AsyncEngine,
) -> None:
    client, actor, base, draft = advance_client
    origin = await confirm_credit(client, actor, base, retained(draft, "50.00"))
    await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = retained(draft)
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    command = {**body, "previewToken": preview["previewToken"]}
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "2"}
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
    before = (
        await client.get(f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers)
    ).json()
    accounts = (await client.get(f"{base}/accounts")).json()
    await _migrate(postgres_engine, revision="20261022_01", downgrade=True)
    await _migrate(postgres_engine, revision="head", downgrade=False)
    assert (await client.get(f"{base}/settlements/{origin['id']}")).json() == origin
    assert (await client.get(f"{base}/accounts")).json() == accounts
    assert (await client.post(f"{base}/settlements/preview", json=body)).json() == preview
    assert (
        await client.get(f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers)
    ).json() == before
    response = await client.post(f"{base}/settlements", json=command, headers=headers)
    assert response.status_code == 201, response.text
    assert (
        await client.post(f"{base}/settlements", json=command, headers=headers)
    ).json() == response.json()
