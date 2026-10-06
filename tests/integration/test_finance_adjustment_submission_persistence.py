"""Adjustment persistence constraints and enforcement-preserving migration."""

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core_console.modules.finance.submission_commands import AdjustmentCommandV1
from core_console.modules.finance.submission_models import FinanceSubmission
from core_console.modules.users.models import User
from integration.finance_submission_helpers import submission_headers
from integration.test_finance_adjustment_submissions import adjustment_body
from integration.test_finance_api import finance_client
from integration.test_finance_nested_submission_persistence import _migrate
from integration.test_finance_transaction_submissions import transaction_body
from integration.test_finance_transaction_submissions import transaction_scope as transaction_scope

pytestmark = pytest.mark.anyio


def _bound(scope: tuple[User, UUID, UUID, UUID]) -> FinanceSubmission:
    actor, ledger, account, _ = scope
    return FinanceSubmission(
        local_user_id=actor.id,
        submission_id=uuid4(),
        command_version="1",
        retention_ledger_id=ledger,
        canonical_command={
            "commandVersion": "1",
            "operation": "createBalanceAdjustment",
            "targetLedgerId": str(ledger),
            "body": AdjustmentCommandV1.model_validate(adjustment_body(account)).canonical(),
        },
    )


@pytest.mark.parametrize(
    "invalid",
    [
        "extra",
        "missing",
        "date",
        "account",
        "nature",
        "note",
        "missing_note",
        "number",
        "precision",
        "negative_zero",
        "leading_zero",
        "currency",
        "structure",
        "expected_precision",
        "retention",
        "no_change_resource",
        "no_change_problem",
        "wrong_resource",
        "wrong_rejection",
        "category_rejection",
        "no_resolved_at",
    ],
)
async def test_postgresql_refuses_noncanonical_adjustment_or_impossible_evidence(
    postgres_session: AsyncSession, transaction_scope: tuple[User, UUID, UUID, UUID], invalid: str
) -> None:
    row = _bound(transaction_scope)
    command = deepcopy(row.canonical_command)
    body = command["body"]
    assert isinstance(body, dict)
    if invalid == "extra":
        body["extra"] = None
    elif invalid == "missing":
        del body["targetBalance"]
    elif invalid == "missing_note":
        del body["note"]
    elif invalid == "date":
        body["transactionDate"] = "2026-02-30"
    elif invalid == "account":
        body["accountId"] = "bad"
    elif invalid == "nature":
        body["expectedAccountNature"] = "other"
    elif invalid == "note":
        body["note"] = " note "
    elif invalid in (
        "number",
        "precision",
        "negative_zero",
        "leading_zero",
        "currency",
        "structure",
        "expected_precision",
    ):
        money = body[
            "expectedDerivedBalance" if invalid == "expected_precision" else "targetBalance"
        ]
        assert isinstance(money, dict)
        if invalid == "currency":
            money["currency"] = "EUR"
        elif invalid == "structure":
            money["extra"] = None
        else:
            money["amount"] = {
                "number": -12.3,
                "precision": "-12.300",
                "expected_precision": "0.000",
                "negative_zero": "-0.00",
                "leading_zero": "-012.30",
            }[invalid]
    elif invalid == "retention":
        row.retention_ledger_id = None
    else:
        row.resolved_at = datetime.now(UTC)
        if invalid == "no_change_resource":
            row.terminal_outcome = {
                "kind": "noChange",
                "resource": {"type": "transaction", "id": str(uuid4())},
            }
        elif invalid == "no_change_problem":
            row.terminal_outcome = {"kind": "noChange", "problem": {}}
        elif invalid == "wrong_resource":
            row.terminal_outcome = {
                "kind": "created",
                "resource": {"type": "account", "id": str(uuid4())},
            }
        elif invalid == "no_resolved_at":
            row.terminal_outcome = {"kind": "noChange"}
            row.resolved_at = None
        else:
            row.terminal_outcome = {
                "kind": "rejected",
                "problem": {
                    "type": "about:blank",
                    "title": "Conflict",
                    "status": 422 if invalid == "wrong_rejection" else 409,
                    "code": "account_balance_changed"
                    if invalid == "wrong_rejection"
                    else "finance_category_archived",
                    "detail": "invalid pairing",
                },
            }
    row.canonical_command = command
    postgres_session.add(row)
    with pytest.raises(DBAPIError):
        await postgres_session.commit()


async def test_no_change_evidence_cannot_be_rebound_or_resolved_again(
    postgres_database_url: str,
    postgres_session: AsyncSession,
    transaction_scope: tuple[User, UUID, UUID, UUID],
) -> None:
    actor, ledger, account, _ = transaction_scope
    key = uuid4()
    headers = submission_headers(actor.id, key)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        receipt = (
            await client.post(
                f"/api/finance/ledgers/{ledger}/balance-adjustments",
                json=adjustment_body(account, target="0"),
                headers=headers,
            )
        ).json()
        changes: list[dict[str, Any]] = [
            {"terminal_outcome": None, "resolved_at": None},
            {"canonical_command": {}},
            {"submission_id": uuid4()},
            {"resolved_at": datetime.now(UTC)},
            {
                "terminal_outcome": {
                    "kind": "created",
                    "resource": {"type": "transaction", "id": str(uuid4())},
                }
            },
        ]
        for change in changes:
            with pytest.raises(IntegrityError):
                await postgres_session.execute(
                    update(FinanceSubmission)
                    .where(FinanceSubmission.submission_id == key)
                    .values(**change)
                )
                await postgres_session.commit()
            await postgres_session.rollback()
        assert (await client.get(f"/api/finance/submissions/{key}", headers=headers)).json()[
            "receipt"
        ] == receipt


@pytest.mark.parametrize("state", ["unfinished", "created", "noChange", "rejected"])
async def test_t08_populated_migration_preserves_prior_evidence_and_blocks_unsafe_downgrade(
    postgres_database_url: str,
    postgres_session: AsyncSession,
    postgres_engine: AsyncEngine,
    transaction_scope: tuple[User, UUID, UUID, UUID],
    state: str,
) -> None:
    actor, ledger, account, other = transaction_scope
    prior_headers = submission_headers(actor.id)
    path = f"/api/finance/ledgers/{ledger}/balance-adjustments"
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        prior = (
            await client.post(
                f"/api/finance/ledgers/{ledger}/transactions",
                json=transaction_body("income", other, account),
                headers=prior_headers,
            )
        ).json()
        positions = (await client.get(f"/api/finance/ledgers/{ledger}/accounts")).json()
    await _migrate(postgres_engine, revision="20261005_01", downgrade=True)
    await _migrate(postgres_engine, revision="head", downgrade=False)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        assert (await client.get(f"/api/finance/ledgers/{ledger}/accounts")).json() == positions
        assert (
            await client.get(
                f"/api/finance/submissions/{prior_headers['Idempotency-Key']}",
                headers=prior_headers,
            )
        ).json()["receipt"] == prior
        row = _bound(transaction_scope)
        key = row.submission_id
        headers = submission_headers(actor.id, key)
        body = adjustment_body(account, target="0" if state == "noChange" else "-12.30")
        if state == "unfinished":
            postgres_session.add(row)
            await postgres_session.commit()
        else:
            if state == "rejected":
                body["expectedAccountNature"] = "liability"
            reply = await client.post(path, json=body, headers=headers)
            assert reply.status_code == (409 if state == "rejected" else 200)
        before = (await client.get(f"/api/finance/submissions/{key}", headers=headers)).json()
        with pytest.raises(DBAPIError, match="enforcement must be preserved"):
            await _migrate(postgres_engine, revision="20261005_01", downgrade=True)
        async with postgres_engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "20261006_01"
            )
        assert (
            await client.get(f"/api/finance/submissions/{key}", headers=headers)
        ).json() == before
        retry = await client.post(path, json=body, headers=headers)
        assert retry.status_code == (409 if state == "rejected" else 200)
        if state != "unfinished":
            assert (
                retry.json()["submissionReceipt"] if state == "rejected" else retry.json()
            ) == before["receipt"]
        else:
            assert retry.json()["admittedAt"] == before["admittedAt"]
        retained = await postgres_session.scalar(
            select(FinanceSubmission).where(FinanceSubmission.submission_id == key)
        )
        assert retained is not None and retained.retention_ledger_id == ledger
