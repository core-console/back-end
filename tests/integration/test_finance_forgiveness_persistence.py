"""Closed forgiveness evidence and lossless migration boundaries in PostgreSQL."""

from copy import deepcopy
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core_console.modules.finance.settlement_models import FinanceSettlementAllocation
from core_console.modules.finance.submission_models import FinanceSubmission
from core_console.modules.users.models import User
from integration.finance_submission_helpers import submission_headers
from integration.test_finance_forgiveness import forgiveness
from integration.test_finance_nested_submission_persistence import _migrate
from integration.test_finance_settlements import advance, advance_client, repayment

pytestmark = pytest.mark.anyio
__all__ = ["advance_client"]


@pytest.mark.parametrize(
    "invalid",
    [
        "kind",
        "cash_account",
        "reason_missing",
        "reason_empty",
        "reason_space",
        "reason_type",
        "selection_missing",
        "selection_empty",
        "selection_duplicate",
        "selection_order",
        "selection_uuid",
        "excess",
        "extra",
        "date",
        "zero",
        "negative",
        "precision",
        "money_type",
        "money_extra",
        "operation",
        "legacy",
        "created_type",
        "rejected_code",
    ],
)
async def test_postgresql_rejects_malformed_forgiveness_evidence(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    invalid: str,
) -> None:
    client, actor, base, draft = advance_client
    first = await advance(client, actor, base, draft, "40.00", "2026-10-20")
    second = await advance(client, actor, base, draft, "60.00", "2026-10-21")
    body = forgiveness(draft, [first["obligation"]["id"], second["obligation"]["id"]], "50.00")
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "5"}
    created = await client.post(
        f"{base}/settlements",
        json={**body, "previewToken": preview["previewToken"]},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    row = await postgres_session.get(
        FinanceSubmission, (actor.id, UUID(headers["Idempotency-Key"]))
    )
    assert row is not None
    command = deepcopy(row.canonical_command)
    intent: Any = command["body"]
    if invalid == "kind":
        intent["kind"] = "cash"
    elif invalid == "cash_account":
        intent["receivingAccountId"] = draft["fundingAccountId"]
    elif invalid == "reason_missing":
        del intent["reason"]
    elif invalid.startswith("reason_"):
        intent["reason"] = {"reason_empty": "", "reason_space": " spaced ", "reason_type": None}[
            invalid
        ]
    elif invalid == "selection_missing":
        del intent["selectedObligationIds"]
    elif invalid.startswith("selection_"):
        ids = intent["selectedObligationIds"]
        intent["selectedObligationIds"] = {
            "selection_empty": [],
            "selection_duplicate": ids * 2,
            "selection_order": list(reversed(ids)),
            "selection_uuid": ["invalid"],
        }[invalid]
    elif invalid == "excess":
        intent["excessDisposition"] = "acceptIncome"
    elif invalid == "extra":
        intent["useCredit"] = True
    elif invalid == "date":
        intent["transactionDate"] = "2026-02-30"
    elif invalid in ("zero", "negative", "precision", "money_type"):
        intent["amount"]["amount"] = {
            "zero": "0.00",
            "negative": "-1.00",
            "precision": "1.001",
            "money_type": 50,
        }[invalid]
    elif invalid == "money_extra":
        intent["amount"]["extra"] = True
    elif invalid == "operation":
        command["operation"] = "createFinanceAdvance"
    elif invalid == "legacy":
        command["commandVersion"] = "4"
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
                "detail": "wrong command evidence",
            },
        }
    row.canonical_command = command
    with pytest.raises(IntegrityError):
        await postgres_session.commit()


@pytest.mark.parametrize("invalid", ["cash", "zero", "negative", "nan", "infinity", "kind"])
async def test_postgresql_requires_typed_non_cash_allocation(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    invalid: str,
) -> None:
    from decimal import Decimal

    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = forgiveness(draft, [source["obligation"]["id"]], "20.00")
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    result = await client.post(
        f"{base}/settlements",
        json={**body, "previewToken": preview["previewToken"]},
        headers={**submission_headers(actor.id), "Finance-Command-Version": "5"},
    )
    assert result.status_code == 201
    row = await postgres_session.scalar(select(FinanceSettlementAllocation))
    assert row is not None
    if invalid == "cash":
        row.cash_paid = Decimal("1")
    elif invalid == "kind":
        row.kind = "cash"
    else:
        row.forgiven = Decimal(
            {"zero": "0", "negative": "-1", "nan": "NaN", "infinity": "Infinity"}[invalid]
        )
    with pytest.raises(IntegrityError):
        await postgres_session.commit()


@pytest.mark.parametrize("state", ["unfinished", "created", "rejected"])
async def test_every_forgiveness_state_blocks_destructive_downgrade(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    postgres_engine: AsyncEngine,
    state: str,
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = forgiveness(draft, [source["obligation"]["id"]], "20.00")
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "5"}
    command = {
        **body,
        "note": None,
        "previewToken": preview["previewToken"] if state == "created" else "0" * 64,
    }
    if state == "unfinished":
        postgres_session.add(
            FinanceSubmission(
                local_user_id=actor.id,
                submission_id=UUID(headers["Idempotency-Key"]),
                command_version="5",
                retention_ledger_id=UUID(base.rsplit("/", 1)[1]),
                canonical_command={
                    "commandVersion": "5",
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
    with pytest.raises(DBAPIError, match="forgiveness evidence"):
        await _migrate(postgres_engine, revision="20261024_01", downgrade=True)
    assert (await client.get(url, headers=headers)).json() == before
    async with postgres_engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "20261025_01"
        )


async def test_populated_t07_upgrade_preserves_legacy_receipts_and_financial_positions(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_engine: AsyncEngine,
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    receipts = []
    for version in ("1", "2", "3", "4"):
        body = repayment(draft, "10.00")
        if version == "2":
            body["excessDisposition"] = "retainCredit"
        elif version == "3":
            body["excessDisposition"] = "acceptIncome"
            body["amount"] = {"amount": "100.00", "currency": "CNY"}
            body["categoryAllocations"] = [{"amount": {"amount": "20.00", "currency": "CNY"}}]
        elif version == "4":
            later = await advance(client, actor, base, draft, "20.00", "2026-10-21")
            body["selectedObligationIds"] = [later["obligation"]["id"]]
        preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
        headers = {**submission_headers(actor.id), "Finance-Command-Version": version}
        result = await client.post(
            f"{base}/settlements",
            json={**body, "previewToken": preview["previewToken"]},
            headers=headers,
        )
        assert result.status_code == 201, result.text
        receipts.append((headers, result.json()))
    paths = [
        "accounts",
        "transactions",
        f"advances/{source['id']}",
        f"participants/{draft['participantId']}/settlements",
    ]
    before = [(await client.get(f"{base}/{path}")).json() for path in paths]
    await _migrate(postgres_engine, revision="20261024_01", downgrade=True)
    await _migrate(postgres_engine, revision="head", downgrade=False)
    assert [(await client.get(f"{base}/{path}")).json() for path in paths] == before
    for headers, receipt in receipts:
        recovered = await client.get(
            f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
        )
        assert recovered.json()["receipt"] == receipt
