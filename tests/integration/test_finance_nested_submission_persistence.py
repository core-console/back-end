"""Closed nested command/outcome constraints and retained migration evidence."""

from copy import deepcopy
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core_console.modules.finance.models import FinanceLedger
from core_console.modules.finance.money import Money
from core_console.modules.finance.service import create_finance_account
from core_console.modules.finance.submission_models import FinanceSubmission
from core_console.modules.users.models import User
from integration.finance_submission_helpers import submission_headers
from integration.test_finance_api import _user, finance_client
from integration.test_finance_nested_submissions import _bound
from integration.test_finance_nested_submissions import nested_scope as nested_scope

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("resource", ["accounts", "categories"])
@pytest.mark.parametrize(
    "invalid",
    [
        "missing_name",
        "null_body",
        "extra_body",
        "wrong_operation",
        "wrong_version",
        "null_target",
        "wrong_retention",
        "missing_retention",
        "foreign_owner",
        "wrong_resource",
        "no_change",
        "missing_resource_id",
        "resolved_without_outcome",
    ],
)
async def test_database_rejects_inconsistent_nested_bindings_and_outcomes(
    postgres_session: AsyncSession,
    nested_scope: tuple[User, UUID],
    resource: str,
    invalid: str,
) -> None:
    actor, ledger_id = nested_scope
    row = _bound(actor, ledger_id, uuid4(), resource)
    command_body = deepcopy(row.canonical_command)
    body = command_body["body"]
    assert isinstance(body, dict)
    if invalid == "missing_name":
        del body["name"]
    elif invalid == "null_body":
        command_body["body"] = None
    elif invalid == "extra_body":
        body["extra"] = None
    elif invalid == "wrong_operation":
        command_body["operation"] = "createFinanceTransaction"
    elif invalid == "wrong_version":
        command_body["commandVersion"] = "99"
    elif invalid == "null_target":
        command_body["targetLedgerId"] = None
    elif invalid == "wrong_retention":
        row.retention_ledger_id = uuid4()
    elif invalid == "missing_retention":
        row.retention_ledger_id = None
    elif invalid == "foreign_owner":
        other = _user("foreign-retention-owner")
        postgres_session.add(other)
        await postgres_session.commit()
        row.local_user_id = other.id
    elif invalid == "resolved_without_outcome":
        row.resolved_at = datetime.now(UTC)
    else:
        row.resolved_at = datetime.now(UTC)
        if invalid == "no_change":
            row.terminal_outcome = {"kind": "noChange"}
        else:
            row.terminal_outcome = {
                "kind": "created",
                "resource": {
                    "type": "transaction" if invalid == "wrong_resource" else resource[:-1],
                    "id": str(uuid4()),
                },
            }
            if invalid == "missing_resource_id":
                row.terminal_outcome = {"kind": "created", "resource": {"type": "account"}}
    row.canonical_command = command_body
    postgres_session.add(row)
    with pytest.raises(IntegrityError):
        await postgres_session.commit()


@pytest.mark.parametrize(
    "invalid",
    [
        "currency_mismatch",
        "amount_number",
        "negative_zero",
        "extra_precision",
        "missing_amount",
        "leading_zero",
        "wrong_nature",
    ],
)
async def test_database_requires_canonical_account_money_and_fields(
    postgres_session: AsyncSession,
    nested_scope: tuple[User, UUID],
    invalid: str,
) -> None:
    actor, ledger_id = nested_scope
    row = _bound(actor, ledger_id, uuid4(), "accounts")
    command_body = deepcopy(row.canonical_command)
    body = command_body["body"]
    assert isinstance(body, dict)
    money = body["openingBalance"]
    assert isinstance(money, dict)
    if invalid == "currency_mismatch":
        money["currency"] = "USD"
    elif invalid == "amount_number":
        money["amount"] = 12.30
    elif invalid == "negative_zero":
        money["amount"] = "-0.00"
    elif invalid == "extra_precision":
        money["amount"] = "12.301"
    elif invalid == "missing_amount":
        del money["amount"]
    elif invalid == "leading_zero":
        money["amount"] = "012.30"
    else:
        body["nature"] = "equity"
    row.canonical_command = command_body
    postgres_session.add(row)
    with pytest.raises(IntegrityError):
        await postgres_session.commit()


async def _migrate(engine: AsyncEngine, *, revision: str, downgrade: bool) -> None:
    async with engine.begin() as connection:

        def apply(sync_connection: Connection) -> None:
            config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
            config.attributes["connection"] = sync_connection
            if downgrade:
                command.downgrade(config, revision)
            else:
                command.upgrade(config, revision)

        await connection.run_sync(apply)


async def test_additive_migration_preserves_old_receipts_positions_and_nested_enforcement(
    postgres_database_url: str,
    postgres_session: AsyncSession,
    postgres_engine: AsyncEngine,
) -> None:
    actor = _user("t04-populated-migration")
    postgres_session.add(actor)
    await postgres_session.commit()
    headers = submission_headers(actor.id)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        ledger = await client.post("/api/finance/ledgers", json={"name": "Home"}, headers=headers)
        receipt = ledger.json()
        ledger_id = UUID(receipt["outcome"]["resource"]["id"])
    await _migrate(postgres_engine, revision="20261003_01", downgrade=True)
    account = await create_finance_account(
        postgres_session,
        owner_id=actor.id,
        ledger_id=ledger_id,
        name="Historical",
        nature="liability",
        currency="USD",
        opening_balance=Money.parse(amount="-123456789012345678901234567890.01", currency="USD"),
        tracking_start_date=date(2026, 1, 1),
    )
    historical_id = account.account.id
    # Keep both terminal and unfinished Ledger evidence while expanding constraints.
    unfinished = FinanceSubmission(
        local_user_id=actor.id,
        submission_id=uuid4(),
        command_version="1",
        canonical_command={
            "commandVersion": "1",
            "operation": "createFinanceLedger",
            "targetLedgerId": None,
            "body": {"name": "Future"},
        },
    )
    postgres_session.add(unfinished)
    await postgres_session.commit()
    await _migrate(postgres_engine, revision="head", downgrade=False)
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        assert (
            await client.get(
                f"/api/finance/submissions/{headers['Idempotency-Key']}", headers=headers
            )
        ).json() == {"state": "terminal", "receipt": receipt}
        path = f"/api/finance/ledgers/{ledger_id}/accounts"
        accounts = (await client.get(path)).json()
        assert accounts[0]["id"] == str(historical_id)
        assert accounts[0]["currentBalance"]["amount"] == "-123456789012345678901234567890.01"
        nested_headers = submission_headers(actor.id)
        nested = await client.post(
            f"/api/finance/ledgers/{ledger_id}/categories",
            json={"name": "Food"},
            headers=nested_headers,
        )
        assert nested.status_code == 201
    with pytest.raises(Exception, match="enforcement must be preserved"):
        await _migrate(postgres_engine, revision="20261003_01", downgrade=True)
    async with postgres_engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "20261006_01"
        )
    async with finance_client(database_url=postgres_database_url, actor=actor) as client:
        assert (
            await client.get(
                f"/api/finance/submissions/{nested_headers['Idempotency-Key']}",
                headers=nested_headers,
            )
        ).json()["receipt"] == nested.json()


async def test_nested_retention_blocks_owner_transfer_and_evidence_rebinding(
    postgres_session: AsyncSession,
    nested_scope: tuple[User, UUID],
) -> None:
    actor, ledger_id = nested_scope
    other = _user("other-owner")
    postgres_session.add(other)
    await postgres_session.commit()
    row = _bound(actor, ledger_id, uuid4(), "accounts")
    postgres_session.add(row)
    await postgres_session.commit()
    with pytest.raises(IntegrityError):
        await postgres_session.execute(
            update(FinanceLedger).where(FinanceLedger.id == ledger_id).values(owner_id=other.id)
        )
        await postgres_session.commit()
    await postgres_session.rollback()
    with pytest.raises(IntegrityError):
        await postgres_session.execute(update(FinanceSubmission).values(retention_ledger_id=None))
        await postgres_session.commit()
    await postgres_session.rollback()
    persisted = await postgres_session.scalar(select(FinanceSubmission))
    assert persisted is not None and persisted.retention_ledger_id == ledger_id
