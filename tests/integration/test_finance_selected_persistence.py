"""Version 4 PostgreSQL guards and lossless migration boundaries."""

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
from integration.test_finance_nested_submission_persistence import _migrate
from integration.test_finance_settlements import advance, advance_client, repayment

pytestmark = pytest.mark.anyio
__all__ = ["advance_client"]


@pytest.mark.parametrize(
    "invalid", ["empty", "duplicate", "type", "uuid", "missing", "extra", "legacy", "operation"]
)
async def test_postgresql_rejects_malformed_selected_command(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    invalid: str,
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "60.00", "2026-10-20")
    body = {**repayment(draft), "selectedObligationIds": [source["obligation"]["id"]]}
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "4"}
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
    if invalid == "empty":
        intent["selectedObligationIds"] = []
    elif invalid == "duplicate":
        intent["selectedObligationIds"] *= 2
    elif invalid == "type":
        intent["selectedObligationIds"] = [None]
    elif invalid == "uuid":
        intent["selectedObligationIds"] = ["not-a-uuid"]
    elif invalid == "missing":
        del intent["selectedObligationIds"]
    elif invalid == "extra":
        intent["useCredit"] = True
    elif invalid == "legacy":
        command["commandVersion"] = "1"
    else:
        command["operation"] = "createFinanceAdvance"
    row.canonical_command = command
    with pytest.raises(IntegrityError):
        await postgres_session.commit()


@pytest.mark.parametrize("state", ["unfinished", "created", "rejected"])
async def test_selected_evidence_blocks_downgrade_without_loss(
    advance_client: tuple[AsyncClient, User, str, dict[str, object]],
    postgres_session: AsyncSession,
    postgres_engine: AsyncEngine,
    state: str,
) -> None:
    client, actor, base, draft = advance_client
    source = await advance(client, actor, base, draft, "60.00", "2026-10-20")
    body = {**repayment(draft), "selectedObligationIds": [source["obligation"]["id"]]}
    preview = (await client.post(f"{base}/settlements/preview", json=body)).json()
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "4"}
    command = {**body, "previewToken": preview["previewToken"] if state == "created" else "0" * 64}
    if state == "unfinished":
        postgres_session.add(
            FinanceSubmission(
                local_user_id=actor.id,
                submission_id=UUID(headers["Idempotency-Key"]),
                command_version="4",
                retention_ledger_id=UUID(base.rsplit("/", 1)[1]),
                canonical_command={
                    "commandVersion": "4",
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
    before = (
        await client.get(f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers)
    ).json()
    with pytest.raises(DBAPIError, match="selected repayment evidence"):
        await _migrate(postgres_engine, revision="20261023_01", downgrade=True)
    assert (
        await client.get(f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers)
    ).json() == before
    async with postgres_engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "20261024_02"
        )
