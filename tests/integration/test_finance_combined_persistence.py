"""Retained combined command grammar and lossless PostgreSQL migration."""

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
from integration.test_finance_combined_settlements import combined
from integration.test_finance_forgiveness import forgiveness
from integration.test_finance_nested_submission_persistence import _migrate
from integration.test_finance_settlements import advance, advance_client

pytestmark = pytest.mark.anyio
__all__ = ["advance_client"]


@pytest.mark.parametrize(
    "invalid",
    [
        "kind",
        "cash_account_missing",
        "reason_missing",
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
        "cash_zero",
        "cash_precision",
        "cash_type",
        "forgiven_zero",
        "forgiven_negative",
        "forgiven_precision",
        "forgiven_type",
        "forgiven_extra",
        "forgiven_missing",
        "currency",
        "operation",
        "legacy",
        "created_type",
        "rejected_code",
    ],
)
async def test_postgresql_rejects_malformed_combined_command_and_outcome(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    invalid: str,
) -> None:
    client, actor, base, draft = advance_client
    first = await advance(client, actor, base, draft, "40.00", "2026-10-20")
    second = await advance(client, actor, base, draft, "60.00", "2026-10-21")
    body = combined(draft, [first["obligation"]["id"], second["obligation"]["id"]])
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "6"}
    result = await client.post(
        f"{base}/settlements",
        json={**body, "previewToken": preview["previewToken"]},
        headers=headers,
    )
    assert result.status_code == 201, result.text
    row = await postgres_session.get(
        FinanceSubmission, (actor.id, UUID(headers["Idempotency-Key"]))
    )
    assert row is not None
    command = deepcopy(row.canonical_command)
    intent: Any = command["body"]
    if invalid == "kind":
        intent["kind"] = "forgiveness"
    elif invalid == "cash_account_missing":
        del intent["receivingAccountId"]
    elif invalid == "reason_missing":
        del intent["reason"]
    elif invalid == "reason_space":
        intent["reason"] = " spaced "
    elif invalid == "reason_type":
        intent["reason"] = None
    elif invalid == "selection_missing":
        del intent["selectedObligationIds"]
    elif invalid.startswith("selection_"):
        ids = intent["selectedObligationIds"]
        intent["selectedObligationIds"] = {
            "empty": [],
            "duplicate": ids * 2,
            "order": list(reversed(ids)),
            "uuid": ["invalid"],
        }[invalid.split("_", 1)[1]]
    elif invalid == "excess":
        intent["excessDisposition"] = "acceptIncome"
    elif invalid == "extra":
        intent["useCredit"] = True
    elif invalid == "date":
        intent["transactionDate"] = "2026-02-30"
    elif invalid.startswith(("cash_", "forgiven_")) and invalid not in (
        "forgiven_missing",
        "forgiven_extra",
    ):
        target = "amount" if invalid.startswith("cash_") else "forgivenessAmount"
        intent[target]["amount"] = {
            "zero": "0.00",
            "negative": "-1.00",
            "precision": "1.001",
            "type": 10,
        }[invalid.split("_", 1)[1]]
    elif invalid == "forgiven_extra":
        intent["forgivenessAmount"]["extra"] = True
    elif invalid == "forgiven_missing":
        del intent["forgivenessAmount"]
    elif invalid == "currency":
        intent["forgivenessAmount"]["currency"] = "USD"
    elif invalid == "operation":
        command["operation"] = "createFinanceAdvance"
    elif invalid == "legacy":
        command["commandVersion"] = "5"
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


@pytest.mark.parametrize("state", ["unfinished", "created", "rejected"])
async def test_all_combined_submission_states_prevent_destructive_downgrade(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    postgres_engine: AsyncEngine,
    state: str,
) -> None:
    client, actor, base, draft = advance_client
    owner_id = actor.id
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = combined(draft, [source["obligation"]["id"]])
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    headers = {**submission_headers(owner_id), "Finance-Command-Version": "6"}
    command = {**body, "previewToken": preview["previewToken"] if state == "created" else "0" * 64}
    if state == "unfinished":
        postgres_session.add(
            FinanceSubmission(
                local_user_id=owner_id,
                submission_id=UUID(headers["Idempotency-Key"]),
                command_version="6",
                retention_ledger_id=UUID(base.rsplit("/", 1)[1]),
                canonical_command={
                    "commandVersion": "6",
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
    url = f"/api/finance/submissions/{headers['Idempotency-Key']}"
    before = (await client.get(url, headers=headers)).json()
    with pytest.raises(DBAPIError, match="combined Settlement evidence"):
        await _migrate(postgres_engine, revision="20261024_02", downgrade=True)
    assert (await client.get(url, headers=headers)).json() == before
    async with postgres_engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "20261025_01"
        )


async def test_populated_t08_upgrade_keeps_forgiveness_and_all_legacy_evidence(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_engine: AsyncEngine,
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "100.00", "2026-10-20")
    body = forgiveness(draft, [source["obligation"]["id"]], "20.00")
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "5"}
    response = await client.post(
        f"{base}/settlements",
        json={**body, "previewToken": preview["previewToken"]},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    paths = [
        "accounts",
        "transactions",
        f"advances/{source['id']}",
        f"participants/{draft['participantId']}/settlements",
    ]
    before = [(await client.get(f"{base}/{path}")).json() for path in paths]
    await _migrate(postgres_engine, revision="20261024_02", downgrade=True)
    await _migrate(postgres_engine, revision="head", downgrade=False)
    assert [(await client.get(f"{base}/{path}")).json() for path in paths] == before
    assert (
        await client.get(f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers)
    ).json()["receipt"] == response.json()
    # The same Obligation can now retain cash and forgiveness in distinct stages.
    body = combined(draft, [source["obligation"]["id"]], "70.00", "10.00")
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    assert (
        await client.post(
            f"{base}/settlements",
            json={**body, "previewToken": preview["previewToken"]},
            headers={**submission_headers(actor.id), "Finance-Command-Version": "6"},
        )
    ).status_code == 201
