"""Retained shared command enforcement and populated T02-to-T03 migration safety."""

from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core_console.modules.finance.submission_models import FinanceSubmission
from core_console.modules.users.models import User
from integration.finance_submission_helpers import submission_headers
from integration.test_finance_advances import advance_client as advance_client
from integration.test_finance_nested_submission_persistence import _migrate
from integration.test_finance_shared_advances import shared_client as shared_client

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "invalid",
    [
        "version",
        "operation",
        "total",
        "owner_negative",
        "owner_currency",
        "owner_precision",
        "null_amount",
        "no_participants",
        "duplicate",
        "order",
        "zero_share",
        "share_currency",
        "extra_share",
        "extra_body",
        "date",
        "token",
        "participant_id",
        "missing_owner",
    ],
)
async def test_postgresql_rejects_invalid_retained_shared_command(
    shared_client: tuple[AsyncClient, User, str, dict[str, Any]],
    postgres_session: AsyncSession,
    invalid: str,
) -> None:
    client, actor, base, draft = shared_client
    preview = (await client.post(f"{base}/advances/preview", json=draft)).json()
    headers = {**submission_headers(actor.id), "Finance-Command-Version": "2"}
    result = await client.post(
        f"{base}/advances", json={**draft, "previewToken": preview["previewToken"]}, headers=headers
    )
    assert result.status_code == 201
    row = await postgres_session.scalar(
        select(FinanceSubmission).where(
            FinanceSubmission.submission_id == UUID(headers["Idempotency-Key"])
        )
    )
    assert row is not None
    command = deepcopy(row.canonical_command)
    body: Any = command["body"]
    if invalid == "version":
        command["commandVersion"] = "1"
    elif invalid == "operation":
        command["operation"] = "createFinanceTransaction"
    elif invalid == "total":
        body["amount"]["amount"] = "299.99"
    elif invalid.startswith("owner_"):
        field, value = {
            "owner_negative": ("amount", "-100.00"),
            "owner_currency": ("currency", "USD"),
            "owner_precision": ("amount", "100.000"),
        }[invalid]
        body["ownerShare"][field] = value
    elif invalid == "null_amount":
        body["amount"] = None
    elif invalid == "no_participants":
        body["participantShares"] = []
    elif invalid == "duplicate":
        body["participantShares"][1]["participantId"] = body["participantShares"][0][
            "participantId"
        ]
    elif invalid == "order":
        body["participantShares"].reverse()
    elif invalid == "zero_share":
        body["participantShares"][0]["amount"]["amount"] = "0.00"
    elif invalid == "share_currency":
        body["participantShares"][0]["amount"]["currency"] = "USD"
    elif invalid == "extra_share":
        body["participantShares"][0]["note"] = "extra"
    elif invalid == "extra_body":
        body["settlement"] = {}
    elif invalid == "date":
        body["transactionDate"] = "2026-02-30"
    elif invalid == "token":
        body["previewToken"] = None
    elif invalid == "participant_id":
        body["participantShares"][0]["participantId"] = 123
    else:
        del body["ownerShare"]
    with pytest.raises(IntegrityError):
        async with postgres_session.begin_nested():
            postgres_session.add(
                FinanceSubmission(
                    local_user_id=actor.id,
                    submission_id=uuid4(),
                    command_version="2",
                    canonical_command=command,
                    retention_ledger_id=UUID(base.rsplit("/", 1)[1]),
                )
            )
            await postgres_session.flush()


async def test_t03_migration_preserves_t02_sources_and_retained_terminal_and_unfinished_actions(
    shared_client: tuple[AsyncClient, User, str, dict[str, Any]],
    postgres_session: AsyncSession,
    postgres_engine: AsyncEngine,
) -> None:
    client, actor, base, shared = shared_client
    draft = {
        "fundingAccountId": shared["fundingAccountId"],
        "participantId": shared["participantShares"][0]["participantId"],
        "transactionDate": "2026-10-20",
        "amount": {"amount": "12.30", "currency": "CNY"},
        "ownerShare": {"amount": "0.00", "currency": "CNY"},
        "participantShare": {"amount": "12.30", "currency": "CNY"},
        "note": None,
    }
    preview = (await client.post(f"{base}/advances/preview", json=draft)).json()
    command = {**draft, "previewToken": preview["previewToken"]}
    headers = submission_headers(actor.id)
    created = await client.post(f"{base}/advances", json=command, headers=headers)
    assert created.status_code == 201
    advance_id = created.json()["outcome"]["resource"]["id"]
    source = (await client.get(f"{base}/advances/{advance_id}")).json()
    positions = (await client.get(f"{base}/accounts")).json()
    refreshed = (await client.post(f"{base}/advances/preview", json=draft)).json()
    pending_command = {**draft, "previewToken": refreshed["previewToken"]}
    row = await postgres_session.scalar(
        select(FinanceSubmission).where(
            FinanceSubmission.submission_id == UUID(headers["Idempotency-Key"])
        )
    )
    assert row is not None
    canonical = deepcopy(row.canonical_command)
    canonical["body"] = pending_command
    pending_key = uuid4()
    postgres_session.add(
        FinanceSubmission(
            local_user_id=actor.id,
            submission_id=pending_key,
            command_version="1",
            canonical_command=canonical,
            retention_ledger_id=UUID(base.rsplit("/", 1)[1]),
        )
    )
    await postgres_session.commit()
    pending_headers = submission_headers(actor.id, pending_key)
    before = (
        await client.get(f"/api/finance/submissions/{pending_key}", headers=pending_headers)
    ).json()
    await _migrate(postgres_engine, revision="20261018_01", downgrade=True)
    await _migrate(postgres_engine, revision="head", downgrade=False)
    assert (await client.get(f"{base}/advances/{advance_id}")).json() == source
    assert (await client.get(f"{base}/accounts")).json() == positions
    assert (
        await client.post(f"{base}/advances", json=command, headers=headers)
    ).json() == created.json()
    assert (
        await client.get(f"/api/finance/submissions/{pending_key}", headers=pending_headers)
    ).json() == before
    resumed = await client.post(f"{base}/advances", json=pending_command, headers=pending_headers)
    assert resumed.status_code == 201, resumed.text
    assert resumed.json()["admittedAt"] == before["admittedAt"]


@pytest.mark.parametrize("state", ["unfinished", "created", "rejected"])
async def test_t03_downgrade_cannot_discard_any_retained_version_two_evidence(
    shared_client: tuple[AsyncClient, User, str, dict[str, Any]],
    postgres_session: AsyncSession,
    postgres_engine: AsyncEngine,
    state: str,
) -> None:
    client, actor, base, draft = shared_client
    preview = (await client.post(f"{base}/advances/preview", json=draft)).json()
    command = {
        **draft,
        "previewToken": preview["previewToken"] if state != "rejected" else "0" * 64,
    }
    key = uuid4()
    headers = {**submission_headers(actor.id, key), "Finance-Command-Version": "2"}
    if state == "unfinished":
        from core_console.modules.finance.advance_schemas import AdvanceCommandV2

        canonical = {
            "commandVersion": "2",
            "operation": "createFinanceAdvance",
            "targetLedgerId": base.rsplit("/", 1)[1],
            "body": AdvanceCommandV2.model_validate(command).canonical(),
        }
        postgres_session.add(
            FinanceSubmission(
                local_user_id=actor.id,
                submission_id=key,
                command_version="2",
                canonical_command=canonical,
                retention_ledger_id=UUID(base.rsplit("/", 1)[1]),
            )
        )
        await postgres_session.commit()
    else:
        created = await client.post(f"{base}/advances", json=command, headers=headers)
        assert created.status_code == (409 if state == "rejected" else 201)
    before = (await client.get(f"/api/finance/submissions/{key}", headers=headers)).json()
    with pytest.raises(DBAPIError, match="retained version 2 Advance evidence"):
        await _migrate(postgres_engine, revision="20261018_01", downgrade=True)
    async with postgres_engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "20261024_01"
        )
    assert (await client.get(f"/api/finance/submissions/{key}", headers=headers)).json() == before
    resumed = await client.post(f"{base}/advances", json=command, headers=headers)
    assert resumed.status_code == (409 if state == "rejected" else 201)
