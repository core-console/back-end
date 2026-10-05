"""Transaction command/outcome enforcement and populated migration safety."""

from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import TypeAdapter
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core_console.modules.finance.submission_commands import TransactionCommandV1
from core_console.modules.finance.submission_models import FinanceSubmission
from core_console.modules.users.models import User
from integration.finance_submission_helpers import submission_headers
from integration.test_finance_api import finance_client
from integration.test_finance_nested_submission_persistence import _migrate
from integration.test_finance_transaction_submissions import transaction_body
from integration.test_finance_transaction_submissions import transaction_scope as transaction_scope

pytestmark = pytest.mark.anyio


def _bound(scope: tuple[User, UUID, UUID, UUID], kind: str) -> FinanceSubmission:
    actor, ledger, source, destination = scope
    body: dict[str, object] = (
        TypeAdapter(TransactionCommandV1)
        .validate_python(transaction_body(kind, source, destination))
        .canonical()
    )
    return FinanceSubmission(
        local_user_id=actor.id,
        submission_id=uuid4(),
        command_version="1",
        retention_ledger_id=ledger,
        canonical_command={
            "commandVersion": "1",
            "operation": "createFinanceTransaction",
            "targetLedgerId": str(ledger),
            "body": body,
        },
    )


@pytest.mark.parametrize("kind", ["income", "expense", "internalTransfer"])
@pytest.mark.parametrize(
    "invalid",
    [
        "extra",
        "missing",
        "null_note",
        "untrimmed_note",
        "bad_date",
        "wrong_uuid",
        "numeric",
        "zero",
        "negative",
        "leading_zero",
        "precision",
        "currency",
        "structure",
        "no_change",
        "resource",
        "wrong_rejection",
        "missing_retention",
    ],
)
async def test_postgresql_rejects_noncanonical_commands_and_impossible_outcomes(
    postgres_session: AsyncSession,
    transaction_scope: tuple[User, UUID, UUID, UUID],
    kind: str,
    invalid: str,
) -> None:
    row = _bound(transaction_scope, kind)
    command = deepcopy(row.canonical_command)
    body = command["body"]
    assert isinstance(body, dict)
    money = body["amount" if kind == "internalTransfer" else "economicAmount"]
    assert isinstance(money, dict)
    if invalid == "extra":
        body["extra"] = None
    elif invalid == "missing":
        del body["kind"]
    elif invalid == "null_note":
        del body["note"]
    elif invalid == "untrimmed_note":
        body["note"] = " note "
    elif invalid == "bad_date":
        body["transactionDate"] = "2026-13-01"
    elif invalid == "wrong_uuid":
        body["sourceAccountId" if kind == "internalTransfer" else "accountId"] = "bad"
    elif invalid in ("numeric", "zero", "negative", "leading_zero", "precision"):
        money["amount"] = {
            "numeric": 12.3,
            "zero": "0.00",
            "negative": "-12.30",
            "leading_zero": "012.30",
            "precision": "12.300",
        }[invalid]
    elif invalid == "currency":
        money["currency"] = "EUR"
    elif invalid == "structure":
        if kind == "internalTransfer":
            body["destinationAccountId"] = body["sourceAccountId"]
        else:
            body["categoryAllocations"] = [
                {"amount": {"amount": "1.00", "currency": "CNY"}, "categoryId": None}
            ]
    elif invalid == "missing_retention":
        row.retention_ledger_id = None
    else:
        row.resolved_at = datetime.now(UTC)
        row.terminal_outcome = (
            {"kind": "noChange"}
            if invalid == "no_change"
            else {"kind": "created", "resource": {"type": "account", "id": str(uuid4())}}
            if invalid == "resource"
            else {
                "kind": "rejected",
                "problem": {
                    "type": "about:blank",
                    "status": 422,
                    "title": "Unprocessable Entity",
                    "code": "finance_account_archived",
                    "detail": "bad pairing",
                },
            }
        )
    row.canonical_command = command
    postgres_session.add(row)
    with pytest.raises(IntegrityError):
        await postgres_session.commit()


async def test_transaction_terminal_evidence_cannot_rebind_or_change(
    postgres_database_url: str,
    postgres_session: AsyncSession,
    transaction_scope: tuple[User, UUID, UUID, UUID],
) -> None:
    actor, ledger, source, destination = transaction_scope
    key = uuid4()
    headers = submission_headers(actor.id, key)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        receipt = (
            await client.post(
                f"/api/finance/ledgers/{ledger}/transactions",
                json=transaction_body("expense", source, destination),
                headers=headers,
            )
        ).json()
        changes: list[dict[str, object]] = [
            {"terminal_outcome": None, "resolved_at": None},
            {"canonical_command": {}},
            {"submission_id": uuid4()},
            {"resolved_at": datetime.now(UTC)},
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


async def test_t06_migration_preserves_prior_evidence_and_positions_and_blocks_unsafe_downgrade(
    postgres_database_url: str,
    postgres_session: AsyncSession,
    postgres_engine: AsyncEngine,
    transaction_scope: tuple[User, UUID, UUID, UUID],
) -> None:
    actor, ledger, source, destination = transaction_scope
    headers = submission_headers(actor.id)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        prior = await client.post(
            f"/api/finance/ledgers/{ledger}/categories", json={"name": "Before"}, headers=headers
        )
        assert prior.status_code == 201
        positions = (await client.get(f"/api/finance/ledgers/{ledger}/accounts")).json()
    await _migrate(postgres_engine, revision="20261004_01", downgrade=True)
    await _migrate(postgres_engine, revision="head", downgrade=False)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        assert (await client.get(f"/api/finance/ledgers/{ledger}/accounts")).json() == positions
        assert (
            await client.get(
                f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
            )
        ).json()["receipt"] == prior.json()
        unfinished = _bound(transaction_scope, "income")
        postgres_session.add(unfinished)
        await postgres_session.commit()
        with pytest.raises(DBAPIError, match="enforcement must be preserved"):
            await _migrate(postgres_engine, revision="20261004_01", downgrade=True)
        async with postgres_engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "20261005_01"
            )
        key = unfinished.submission_id
        resumed = await client.post(
            f"/api/finance/ledgers/{ledger}/transactions",
            json=transaction_body("income", source, destination),
            headers=submission_headers(actor.id, key),
        )
        assert resumed.status_code == 201
        row = await postgres_session.scalar(
            select(FinanceSubmission).where(FinanceSubmission.submission_id == key)
        )
        assert row is not None and row.retention_ledger_id == ledger
