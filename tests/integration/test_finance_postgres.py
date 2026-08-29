"""Real PostgreSQL coverage for Finance Ledger persistence."""

import asyncio
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TypedDict
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, func, inspect, select, text
from sqlalchemy.engine.interfaces import (
    ReflectedCheckConstraint,
    ReflectedColumn,
    ReflectedForeignKeyConstraint,
    ReflectedIndex,
    ReflectedPrimaryKeyConstraint,
    ReflectedUniqueConstraint,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from core_console.modules.finance.models import (
    FinanceAccount,
    FinanceAccountMovement,
    FinanceCategory,
    FinanceCategoryAllocation,
    FinanceLedger,
    FinanceTransaction,
)
from core_console.modules.finance.money import Money
from core_console.modules.finance.queries import FinanceTransactionDetail
from core_console.modules.finance.service import (
    BalanceAdjustmentResult,
    FinanceAccountBalanceChangedError,
    FinanceAccountSemanticsLockedError,
    FinanceTransactionNotFoundError,
    correct_finance_account_semantics,
    create_balance_adjustment,
    create_finance_transaction,
    create_internal_transfer_transaction,
    delete_finance_transaction,
    replace_balance_adjustment,
    replace_finance_transaction,
    replace_internal_transfer_transaction,
)
from core_console.modules.users.models import User

pytestmark = pytest.mark.anyio

ALEMBIC_CONFIG_PATH = Path(__file__).resolve().parents[2] / "alembic.ini"


@pytest.mark.parametrize(
    ("source_nature", "destination_nature", "expected_source", "expected_destination"),
    [
        ("asset", "asset", Decimal("-10.00"), Decimal("10.00")),
        ("asset", "liability", Decimal("-10.00"), Decimal("-10.00")),
        ("liability", "asset", Decimal("10.00"), Decimal("10.00")),
        ("liability", "liability", Decimal("10.00"), Decimal("-10.00")),
    ],
)
async def test_internal_transfer_persists_exact_role_aware_nature_matrix(
    postgres_session: AsyncSession,
    source_nature: str,
    destination_nature: str,
    expected_source: Decimal,
    expected_destination: Decimal,
) -> None:
    owner = _user(f"transfer-{source_nature}-{destination_nature}")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    source = _account(
        ledger.id,
        name="Source",
        name_key="source",
        nature=source_nature,
        currency="CNY",
        opening_balance=Decimal("5.00"),
        status="active",
    )
    destination = _account(
        ledger.id,
        name="Destination",
        name_key="destination",
        nature=destination_nature,
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add_all([source, destination])
    await postgres_session.commit()

    transaction_id = await create_internal_transfer_transaction(
        postgres_session,
        owner_id=owner.id,
        ledger_id=ledger.id,
        source_account_id=source.id,
        destination_account_id=destination.id,
        transaction_date=date(2030, 8, 1),
        amount=Money.parse(amount="10.00", currency="CNY"),
        note=" transfer ",
        project=lambda detail: detail.transaction.id,
    )

    movements = (
        await postgres_session.scalars(
            select(FinanceAccountMovement)
            .where(FinanceAccountMovement.transaction_id == transaction_id)
            .order_by(FinanceAccountMovement.role)
        )
    ).all()
    allocations = await postgres_session.scalar(
        select(func.count())
        .select_from(FinanceCategoryAllocation)
        .where(FinanceCategoryAllocation.transaction_id == transaction_id)
    )
    assert [(movement.role, movement.amount) for movement in movements] == [
        ("destination", expected_destination),
        ("source", expected_source),
    ]
    assert allocations == 0


@pytest.mark.parametrize(
    ("destination_amount", "destination_currency", "add_allocation", "late_tracking"),
    [
        (None, "CNY", False, False),
        (Decimal("9.00"), "CNY", False, False),
        (Decimal("10.00"), "USD", False, False),
        (Decimal("10.00"), "CNY", True, False),
        (Decimal("10.00"), "CNY", False, True),
    ],
)
async def test_database_rejects_malformed_internal_transfer_aggregates(
    postgres_session: AsyncSession,
    destination_amount: Decimal | None,
    destination_currency: str,
    add_allocation: bool,
    late_tracking: bool,
) -> None:
    owner = _user(uuid4().hex)
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    source = _account(
        ledger.id,
        name="Source",
        name_key="source",
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    destination = _account(
        ledger.id,
        name="Destination",
        name_key="destination",
        nature="asset",
        currency=destination_currency,
        opening_balance=Decimal("0.00"),
        status="active",
    )
    if late_tracking:
        destination.tracking_start_date = date(2026, 8, 2)
    postgres_session.add_all([source, destination])
    await postgres_session.flush()
    transaction = FinanceTransaction(
        ledger_id=ledger.id,
        kind="internal_transfer",
        transaction_date=date(2026, 8, 1),
        note=None,
    )
    postgres_session.add(transaction)
    await postgres_session.flush()
    children: list[FinanceAccountMovement | FinanceCategoryAllocation] = [
        FinanceAccountMovement(
            transaction_id=transaction.id,
            ledger_id=ledger.id,
            account_id=source.id,
            amount=Decimal("-10.00"),
            currency="CNY",
            role="source",
        )
    ]
    if destination_amount is not None:
        children.append(
            FinanceAccountMovement(
                transaction_id=transaction.id,
                ledger_id=ledger.id,
                account_id=destination.id,
                amount=destination_amount,
                currency=destination_currency,
                role="destination",
            )
        )
    if add_allocation:
        children.append(
            FinanceCategoryAllocation(
                transaction_id=transaction.id,
                ledger_id=ledger.id,
                category_id=None,
                amount=Decimal("10.00"),
                currency="CNY",
            )
        )
    postgres_session.add_all(children)

    with pytest.raises(IntegrityError):
        await postgres_session.commit()


async def test_internal_transfer_projection_failure_rolls_back_all_rows(
    postgres_session: AsyncSession,
) -> None:
    owner = _user("transfer-projection-failure")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    source = _account(
        ledger.id,
        name="Source",
        name_key="source",
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    destination = _account(
        ledger.id,
        name="Destination",
        name_key="destination",
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add_all([source, destination])
    await postgres_session.commit()

    def fail_projection(_: FinanceTransactionDetail) -> None:
        raise RuntimeError("response projection failed")

    with pytest.raises(RuntimeError, match="response projection failed"):
        await create_internal_transfer_transaction(
            postgres_session,
            owner_id=owner.id,
            ledger_id=ledger.id,
            source_account_id=source.id,
            destination_account_id=destination.id,
            transaction_date=date(2026, 8, 1),
            amount=Money.parse(amount="10.00", currency="CNY"),
            note=None,
            project=fail_projection,
        )

    counts = [
        await postgres_session.scalar(select(func.count()).select_from(model))
        for model in (FinanceTransaction, FinanceAccountMovement, FinanceCategoryAllocation)
    ]
    assert counts == [0, 0, 0]


async def test_internal_transfer_history_locks_both_account_semantics(
    postgres_session: AsyncSession,
) -> None:
    owner = _user("transfer-semantics-lock")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    source = _account(
        ledger.id,
        name="Source",
        name_key="source",
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    destination = _account(
        ledger.id,
        name="Destination",
        name_key="destination",
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add_all([source, destination])
    await postgres_session.commit()
    await create_internal_transfer_transaction(
        postgres_session,
        owner_id=owner.id,
        ledger_id=ledger.id,
        source_account_id=source.id,
        destination_account_id=destination.id,
        transaction_date=date(2026, 8, 1),
        amount=Money.parse(amount="1.00", currency="CNY"),
        note=None,
        project=lambda detail: detail.transaction.id,
    )

    owner_id = owner.id
    ledger_id = ledger.id
    account_ids = (source.id, destination.id)
    for account_id in account_ids:
        with pytest.raises(FinanceAccountSemanticsLockedError):
            await correct_finance_account_semantics(
                postgres_session,
                owner_id=owner_id,
                ledger_id=ledger_id,
                account_id=account_id,
                nature="liability",
                currency=None,
            )
        await postgres_session.rollback()


async def test_opposing_internal_transfers_use_compatible_account_lock_order(
    postgres_engine: AsyncEngine,
    postgres_session: AsyncSession,
) -> None:
    owner = _user("opposing-transfer-locks")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    first = _account(
        ledger.id,
        name="First",
        name_key="first",
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    second = _account(
        ledger.id,
        name="Second",
        name_key="second",
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add_all([first, second])
    await postgres_session.commit()

    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with session_factory() as forward_session, session_factory() as reverse_session:
        await asyncio.wait_for(
            asyncio.gather(
                create_internal_transfer_transaction(
                    forward_session,
                    owner_id=owner.id,
                    ledger_id=ledger.id,
                    source_account_id=first.id,
                    destination_account_id=second.id,
                    transaction_date=date(2026, 8, 1),
                    amount=Money.parse(amount="3.00", currency="CNY"),
                    note=None,
                    project=lambda detail: detail.transaction.id,
                ),
                create_internal_transfer_transaction(
                    reverse_session,
                    owner_id=owner.id,
                    ledger_id=ledger.id,
                    source_account_id=second.id,
                    destination_account_id=first.id,
                    transaction_date=date(2026, 8, 1),
                    amount=Money.parse(amount="2.00", currency="CNY"),
                    note=None,
                    project=lambda detail: detail.transaction.id,
                ),
            ),
            timeout=5,
        )

    movement_count = await postgres_session.scalar(
        select(func.count()).select_from(FinanceAccountMovement)
    )
    assert movement_count == 4


async def test_concurrent_ordinary_replacements_of_one_transaction_remain_complete(
    postgres_engine: AsyncEngine,
    postgres_session: AsyncSession,
) -> None:
    owner = _user("ordinary-replace-replace")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add(account)
    await postgres_session.commit()
    transaction_id = await create_finance_transaction(
        postgres_session,
        owner_id=owner.id,
        ledger_id=ledger.id,
        kind="income",
        account_id=account.id,
        transaction_date=date(2026, 8, 1),
        economic_amount=Money.parse(amount="1.00", currency="CNY"),
        allocation_amount=Money.parse(amount="1.00", currency="CNY"),
        category_id=None,
        note=None,
        project=lambda detail: detail.transaction.id,
    )

    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with (
        session_factory() as blocker,
        session_factory() as first,
        session_factory() as second,
        session_factory() as observer,
    ):
        assert await blocker.get(FinanceTransaction, transaction_id, with_for_update=True)
        first_pid = await first.scalar(select(func.pg_backend_pid()))
        second_pid = await second.scalar(select(func.pg_backend_pid()))
        assert first_pid is not None and second_pid is not None

        async def replace(session: AsyncSession, amount: str) -> UUID:
            return await replace_finance_transaction(
                session,
                owner_id=owner.id,
                ledger_id=ledger.id,
                transaction_id=transaction_id,
                kind="income",
                account_id=account.id,
                transaction_date=date(2026, 8, 2),
                economic_amount=Money.parse(amount=amount, currency="CNY"),
                allocation_amount=Money.parse(amount=amount, currency="CNY"),
                category_id=None,
                note=amount,
                project=lambda detail: detail.transaction.id,
            )

        first_task = asyncio.create_task(replace(first, "2.00"))
        assert await _wait_for_postgres_backend_lock(observer, backend_pid=first_pid)
        second_task = asyncio.create_task(replace(second, "3.00"))
        assert await _wait_for_postgres_backend_lock(observer, backend_pid=second_pid)
        await blocker.commit()
        results = await asyncio.wait_for(
            asyncio.gather(first_task, second_task, return_exceptions=True),
            timeout=5,
        )

    assert all(result == transaction_id for result in results)
    transactions = (await postgres_session.scalars(select(FinanceTransaction))).all()
    movements = (await postgres_session.scalars(select(FinanceAccountMovement))).all()
    allocations = (await postgres_session.scalars(select(FinanceCategoryAllocation))).all()
    assert len(transactions) == len(movements) == len(allocations) == 1
    assert transactions[0].id == transaction_id
    assert movements[0].amount in {Decimal("2.00"), Decimal("3.00")}
    assert allocations[0].amount == abs(movements[0].amount)


async def test_out_of_scope_mutation_does_not_contend_on_owned_transaction_lock(
    postgres_engine: AsyncEngine,
    postgres_session: AsyncSession,
) -> None:
    owner = _user("ordinary-mutation-scope-lock")
    postgres_session.add(owner)
    await postgres_session.flush()
    owned_ledger = _ledger(owner.id, name="Owned", name_key="owned")
    other_ledger = _ledger(owner.id, name="Other", name_key="other")
    postgres_session.add_all([owned_ledger, other_ledger])
    await postgres_session.flush()
    account = _account(
        owned_ledger.id,
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add(account)
    await postgres_session.commit()
    transaction_id = await create_finance_transaction(
        postgres_session,
        owner_id=owner.id,
        ledger_id=owned_ledger.id,
        kind="income",
        account_id=account.id,
        transaction_date=date(2026, 8, 1),
        economic_amount=Money.parse(amount="1.00", currency="CNY"),
        allocation_amount=Money.parse(amount="1.00", currency="CNY"),
        category_id=None,
        note=None,
        project=lambda detail: detail.transaction.id,
    )

    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with (
        session_factory() as blocker,
        session_factory() as owner_session,
        session_factory() as hidden_session,
        session_factory() as observer,
    ):
        assert await blocker.get(FinanceTransaction, transaction_id, with_for_update=True)
        owner_pid = await owner_session.scalar(select(func.pg_backend_pid()))
        assert owner_pid is not None
        owner_task = asyncio.create_task(
            replace_finance_transaction(
                owner_session,
                owner_id=owner.id,
                ledger_id=owned_ledger.id,
                transaction_id=transaction_id,
                kind="income",
                account_id=account.id,
                transaction_date=date(2026, 8, 2),
                economic_amount=Money.parse(amount="2.00", currency="CNY"),
                allocation_amount=Money.parse(amount="2.00", currency="CNY"),
                category_id=None,
                note=None,
                project=lambda detail: detail.transaction.id,
            )
        )
        assert await _wait_for_postgres_backend_lock(observer, backend_pid=owner_pid)
        with pytest.raises(FinanceTransactionNotFoundError):
            await asyncio.wait_for(
                delete_finance_transaction(
                    hidden_session,
                    owner_id=owner.id,
                    ledger_id=other_ledger.id,
                    transaction_id=transaction_id,
                ),
                timeout=1,
            )
        await blocker.commit()
        assert await owner_task == transaction_id


async def test_concurrent_replace_and_delete_leave_no_partial_transaction(
    postgres_engine: AsyncEngine,
    postgres_session: AsyncSession,
) -> None:
    owner = _user("ordinary-replace-delete")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add(account)
    await postgres_session.commit()
    transaction_id = await create_finance_transaction(
        postgres_session,
        owner_id=owner.id,
        ledger_id=ledger.id,
        kind="expense",
        account_id=account.id,
        transaction_date=date(2026, 8, 1),
        economic_amount=Money.parse(amount="1.00", currency="CNY"),
        allocation_amount=Money.parse(amount="1.00", currency="CNY"),
        category_id=None,
        note=None,
        project=lambda detail: detail.transaction.id,
    )

    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with (
        session_factory() as blocker,
        session_factory() as replace_session,
        session_factory() as delete_session,
        session_factory() as observer,
    ):
        assert await blocker.get(FinanceTransaction, transaction_id, with_for_update=True)
        replace_pid = await replace_session.scalar(select(func.pg_backend_pid()))
        delete_pid = await delete_session.scalar(select(func.pg_backend_pid()))
        assert replace_pid is not None and delete_pid is not None
        replace_task = asyncio.create_task(
            replace_finance_transaction(
                replace_session,
                owner_id=owner.id,
                ledger_id=ledger.id,
                transaction_id=transaction_id,
                kind="expense",
                account_id=account.id,
                transaction_date=date(2026, 8, 2),
                economic_amount=Money.parse(amount="2.00", currency="CNY"),
                allocation_amount=Money.parse(amount="2.00", currency="CNY"),
                category_id=None,
                note=None,
                project=lambda detail: detail.transaction.id,
            )
        )
        assert await _wait_for_postgres_backend_lock(observer, backend_pid=replace_pid)
        delete_task = asyncio.create_task(
            delete_finance_transaction(
                delete_session,
                owner_id=owner.id,
                ledger_id=ledger.id,
                transaction_id=transaction_id,
            )
        )
        assert await _wait_for_postgres_backend_lock(observer, backend_pid=delete_pid)
        await blocker.commit()
        results = await asyncio.wait_for(
            asyncio.gather(replace_task, delete_task, return_exceptions=True),
            timeout=5,
        )

    assert any(result is None for result in results)
    assert all(
        result is None
        or result == transaction_id
        or isinstance(result, FinanceTransactionNotFoundError)
        for result in results
    )
    counts = [
        await postgres_session.scalar(select(func.count()).select_from(model))
        for model in (FinanceTransaction, FinanceAccountMovement, FinanceCategoryAllocation)
    ]
    assert counts == [0, 0, 0]


async def test_concurrent_deletes_have_one_authoritative_winner(
    postgres_engine: AsyncEngine,
    postgres_session: AsyncSession,
) -> None:
    owner = _user("ordinary-delete-delete")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add(account)
    await postgres_session.commit()
    transaction_id = await create_finance_transaction(
        postgres_session,
        owner_id=owner.id,
        ledger_id=ledger.id,
        kind="income",
        account_id=account.id,
        transaction_date=date(2026, 8, 1),
        economic_amount=Money.parse(amount="1.00", currency="CNY"),
        allocation_amount=Money.parse(amount="1.00", currency="CNY"),
        category_id=None,
        note=None,
        project=lambda detail: detail.transaction.id,
    )

    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with session_factory() as first, session_factory() as second:
        results = await asyncio.wait_for(
            asyncio.gather(
                delete_finance_transaction(
                    first,
                    owner_id=owner.id,
                    ledger_id=ledger.id,
                    transaction_id=transaction_id,
                ),
                delete_finance_transaction(
                    second,
                    owner_id=owner.id,
                    ledger_id=ledger.id,
                    transaction_id=transaction_id,
                ),
                return_exceptions=True,
            ),
            timeout=5,
        )

    assert sum(result is None for result in results) == 1
    failures = [result for result in results if isinstance(result, Exception)]
    assert len(failures) == 1
    assert isinstance(failures[0], FinanceTransactionNotFoundError)
    assert await postgres_session.scalar(select(func.count()).select_from(FinanceTransaction)) == 0


async def test_opposing_ordinary_replacements_lock_old_and_new_accounts_in_uuid_order(
    postgres_engine: AsyncEngine,
    postgres_session: AsyncSession,
) -> None:
    owner = _user("ordinary-opposing-account-locks")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    first = _account(
        ledger.id,
        name="First",
        name_key="first",
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    second = _account(
        ledger.id,
        name="Second",
        name_key="second",
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add_all([first, second])
    await postgres_session.commit()

    async def create_old(account_id: UUID, amount: str) -> UUID:
        return await create_finance_transaction(
            postgres_session,
            owner_id=owner.id,
            ledger_id=ledger.id,
            kind="income",
            account_id=account_id,
            transaction_date=date(2026, 8, 1),
            economic_amount=Money.parse(amount=amount, currency="CNY"),
            allocation_amount=Money.parse(amount=amount, currency="CNY"),
            category_id=None,
            note=None,
            project=lambda detail: detail.transaction.id,
        )

    first_id = await create_old(first.id, "1.00")
    second_id = await create_old(second.id, "2.00")
    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)

    async def replace(session: AsyncSession, transaction_id: UUID, account_id: UUID) -> UUID:
        return await replace_finance_transaction(
            session,
            owner_id=owner.id,
            ledger_id=ledger.id,
            transaction_id=transaction_id,
            kind="income",
            account_id=account_id,
            transaction_date=date(2026, 8, 2),
            economic_amount=Money.parse(amount="3.00", currency="CNY"),
            allocation_amount=Money.parse(amount="3.00", currency="CNY"),
            category_id=None,
            note=None,
            project=lambda detail: detail.transaction.id,
        )

    async with session_factory() as forward, session_factory() as reverse:
        results = await asyncio.wait_for(
            asyncio.gather(
                replace(forward, first_id, second.id),
                replace(reverse, second_id, first.id),
            ),
            timeout=5,
        )

    assert set(results) == {first_id, second_id}
    movements = (await postgres_session.scalars(select(FinanceAccountMovement))).all()
    assert len(movements) == 2
    assert {movement.account_id for movement in movements} == {first.id, second.id}
    assert {movement.amount for movement in movements} == {Decimal("3.00")}


async def test_opposing_transfer_replacements_lock_four_account_unions_without_deadlock(
    postgres_engine: AsyncEngine,
    postgres_session: AsyncSession,
) -> None:
    owner = _user("transfer-replacement-lock-unions")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    accounts = [
        _account(
            ledger.id,
            name=f"Account {index}",
            name_key=f"account-{index}",
            nature="asset",
            currency="CNY",
            opening_balance=Decimal("0.00"),
            status="active",
        )
        for index in range(4)
    ]
    postgres_session.add_all(accounts)
    await postgres_session.commit()
    first_id = await create_internal_transfer_transaction(
        postgres_session,
        owner_id=owner.id,
        ledger_id=ledger.id,
        source_account_id=accounts[0].id,
        destination_account_id=accounts[1].id,
        transaction_date=date(2026, 8, 1),
        amount=Money.parse(amount="1.00", currency="CNY"),
        note=None,
        project=lambda detail: detail.transaction.id,
    )
    second_id = await create_internal_transfer_transaction(
        postgres_session,
        owner_id=owner.id,
        ledger_id=ledger.id,
        source_account_id=accounts[2].id,
        destination_account_id=accounts[3].id,
        transaction_date=date(2026, 8, 1),
        amount=Money.parse(amount="2.00", currency="CNY"),
        note=None,
        project=lambda detail: detail.transaction.id,
    )
    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)

    async def replace(
        session: AsyncSession,
        transaction_id: UUID,
        source_account_id: UUID,
        destination_account_id: UUID,
    ) -> UUID:
        return await replace_internal_transfer_transaction(
            session,
            owner_id=owner.id,
            ledger_id=ledger.id,
            transaction_id=transaction_id,
            source_account_id=source_account_id,
            destination_account_id=destination_account_id,
            transaction_date=date(2026, 8, 2),
            amount=Money.parse(amount="3.00", currency="CNY"),
            note=None,
            project=lambda detail: detail.transaction.id,
        )

    async with session_factory() as forward, session_factory() as reverse:
        results = await asyncio.wait_for(
            asyncio.gather(
                replace(forward, first_id, accounts[2].id, accounts[3].id),
                replace(reverse, second_id, accounts[0].id, accounts[1].id),
            ),
            timeout=5,
        )

    assert set(results) == {first_id, second_id}
    movements = (await postgres_session.scalars(select(FinanceAccountMovement))).all()
    assert len(movements) == 4
    assert {movement.account_id for movement in movements} == {account.id for account in accounts}


async def test_generic_delete_serializes_with_specialized_adjustment_replacement(
    postgres_engine: AsyncEngine,
    postgres_session: AsyncSession,
) -> None:
    owner = _user("delete-adjustment-race")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add(account)
    await postgres_session.commit()
    created = await create_balance_adjustment(
        postgres_session,
        owner_id=owner.id,
        ledger_id=ledger.id,
        account_id=account.id,
        transaction_date=date(2026, 8, 1),
        expected_derived_balance=Money.parse(amount="0.00", currency="CNY"),
        expected_account_nature="asset",
        target_balance=Money.parse(amount="1.00", currency="CNY"),
        note=None,
        project=lambda result: result,
    )
    assert created.transaction is not None
    transaction_id = created.transaction.transaction.id
    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with session_factory() as replace_session, session_factory() as delete_session:
        results = await asyncio.wait_for(
            asyncio.gather(
                replace_balance_adjustment(
                    replace_session,
                    owner_id=owner.id,
                    ledger_id=ledger.id,
                    transaction_id=transaction_id,
                    account_id=account.id,
                    transaction_date=date(2026, 8, 1),
                    expected_derived_balance=Money.parse(amount="0.00", currency="CNY"),
                    expected_account_nature="asset",
                    target_balance=Money.parse(amount="2.00", currency="CNY"),
                    note=None,
                    project=lambda result: result,
                ),
                delete_finance_transaction(
                    delete_session,
                    owner_id=owner.id,
                    ledger_id=ledger.id,
                    transaction_id=transaction_id,
                ),
                return_exceptions=True,
            ),
            timeout=5,
        )

    assert any(result is None for result in results)
    assert all(
        result is None
        or isinstance(result, (BalanceAdjustmentResult, FinanceTransactionNotFoundError))
        for result in results
    )
    assert await postgres_session.scalar(select(func.count()).select_from(FinanceTransaction)) == 0


@pytest.mark.parametrize(
    "intervening_writer",
    ("income", "expense", "transfer", "adjustment"),
)
async def test_balance_adjustment_recomputes_after_concurrent_account_writer(
    postgres_engine: AsyncEngine,
    postgres_session: AsyncSession,
    intervening_writer: str,
) -> None:
    owner = _user(f"adjustment-concurrency-{intervening_writer}")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        name="Primary",
        name_key="primary",
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    other = _account(
        ledger.id,
        name="Other",
        name_key="other",
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add_all([account, other])
    await postgres_session.commit()

    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with (
        session_factory() as writer_session,
        session_factory() as adjustment_session,
        session_factory() as observer_session,
    ):
        locked_account = await writer_session.get(
            FinanceAccount,
            account.id,
            with_for_update=True,
        )
        assert locked_account is not None
        adjustment_backend_pid = await adjustment_session.scalar(select(func.pg_backend_pid()))
        assert adjustment_backend_pid is not None
        adjustment_task = asyncio.create_task(
            create_balance_adjustment(
                adjustment_session,
                owner_id=owner.id,
                ledger_id=ledger.id,
                account_id=account.id,
                transaction_date=date(2026, 8, 1),
                expected_derived_balance=Money.parse(amount="0.00", currency="CNY"),
                expected_account_nature="asset",
                target_balance=Money.parse(amount="5.00", currency="CNY"),
                note=None,
                project=lambda result: result,
            )
        )
        assert await _wait_for_postgres_backend_lock(
            observer_session,
            backend_pid=adjustment_backend_pid,
        )
        if intervening_writer == "transfer":
            await create_internal_transfer_transaction(
                writer_session,
                owner_id=owner.id,
                ledger_id=ledger.id,
                source_account_id=account.id,
                destination_account_id=other.id,
                transaction_date=date(2026, 8, 1),
                amount=Money.parse(amount="1.00", currency="CNY"),
                note=None,
                project=lambda detail: detail.transaction.id,
            )
        elif intervening_writer in {"income", "expense"}:
            await create_finance_transaction(
                writer_session,
                owner_id=owner.id,
                ledger_id=ledger.id,
                kind=intervening_writer,  # type: ignore[arg-type]
                account_id=account.id,
                transaction_date=date(2026, 8, 1),
                economic_amount=Money.parse(amount="1.00", currency="CNY"),
                allocation_amount=Money.parse(amount="1.00", currency="CNY"),
                category_id=None,
                note=None,
                project=lambda detail: detail.transaction.id,
            )
        else:
            await create_balance_adjustment(
                writer_session,
                owner_id=owner.id,
                ledger_id=ledger.id,
                account_id=account.id,
                transaction_date=date(2026, 8, 1),
                expected_derived_balance=Money.parse(amount="0.00", currency="CNY"),
                expected_account_nature="asset",
                target_balance=Money.parse(amount="1.00", currency="CNY"),
                note=None,
                project=lambda result: result,
            )
        with pytest.raises(FinanceAccountBalanceChangedError):
            await adjustment_task
        await adjustment_session.rollback()

    assert await postgres_session.scalar(select(func.count()).select_from(FinanceTransaction)) == 1


@pytest.mark.parametrize(
    "intervening_writer",
    ("income", "expense", "transfer", "adjustment"),
)
async def test_balance_adjustment_replacement_recomputes_after_concurrent_account_writer(
    postgres_engine: AsyncEngine,
    postgres_session: AsyncSession,
    intervening_writer: str,
) -> None:
    owner = _user(f"replacement-concurrency-{intervening_writer}")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        name="Primary",
        name_key="primary",
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    other = _account(
        ledger.id,
        name="Other",
        name_key="other",
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add_all([account, other])
    await postgres_session.commit()
    old = await create_balance_adjustment(
        postgres_session,
        owner_id=owner.id,
        ledger_id=ledger.id,
        account_id=account.id,
        transaction_date=date(2026, 8, 1),
        expected_derived_balance=Money.parse(amount="0.00", currency="CNY"),
        expected_account_nature="asset",
        target_balance=Money.parse(amount="1.00", currency="CNY"),
        note=None,
        project=lambda result: result,
    )
    assert old.transaction is not None
    old_id = old.transaction.transaction.id

    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with (
        session_factory() as writer_session,
        session_factory() as replacement_session,
        session_factory() as observer_session,
    ):
        assert await writer_session.get(FinanceAccount, account.id, with_for_update=True)
        replacement_pid = await replacement_session.scalar(select(func.pg_backend_pid()))
        assert replacement_pid is not None
        replacement_task = asyncio.create_task(
            replace_balance_adjustment(
                replacement_session,
                owner_id=owner.id,
                ledger_id=ledger.id,
                transaction_id=old_id,
                account_id=account.id,
                transaction_date=date(2026, 8, 1),
                expected_derived_balance=Money.parse(amount="0.00", currency="CNY"),
                expected_account_nature="asset",
                target_balance=Money.parse(amount="3.00", currency="CNY"),
                note=None,
                project=lambda result: result,
            )
        )
        assert await _wait_for_postgres_backend_lock(observer_session, backend_pid=replacement_pid)
        if intervening_writer == "transfer":
            await create_internal_transfer_transaction(
                writer_session,
                owner_id=owner.id,
                ledger_id=ledger.id,
                source_account_id=account.id,
                destination_account_id=other.id,
                transaction_date=date(2026, 8, 1),
                amount=Money.parse(amount="1.00", currency="CNY"),
                note=None,
                project=lambda detail: detail.transaction.id,
            )
        elif intervening_writer in {"income", "expense"}:
            await create_finance_transaction(
                writer_session,
                owner_id=owner.id,
                ledger_id=ledger.id,
                kind=intervening_writer,  # type: ignore[arg-type]
                account_id=account.id,
                transaction_date=date(2026, 8, 1),
                economic_amount=Money.parse(amount="1.00", currency="CNY"),
                allocation_amount=Money.parse(amount="1.00", currency="CNY"),
                category_id=None,
                note=None,
                project=lambda detail: detail.transaction.id,
            )
        else:
            await create_balance_adjustment(
                writer_session,
                owner_id=owner.id,
                ledger_id=ledger.id,
                account_id=account.id,
                transaction_date=date(2026, 8, 1),
                expected_derived_balance=Money.parse(amount="1.00", currency="CNY"),
                expected_account_nature="asset",
                target_balance=Money.parse(amount="2.00", currency="CNY"),
                note=None,
                project=lambda result: result,
            )
        with pytest.raises(FinanceAccountBalanceChangedError):
            await replacement_task
        await replacement_session.rollback()

    assert await postgres_session.scalar(select(func.count()).select_from(FinanceTransaction)) == 2


async def test_changed_account_replacements_lock_overlapping_accounts_in_uuid_order(
    postgres_engine: AsyncEngine,
    postgres_session: AsyncSession,
) -> None:
    owner = _user("changed-account-lock-order")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    first = _account(
        ledger.id,
        name="First",
        name_key="first",
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    second = _account(
        ledger.id,
        name="Second",
        name_key="second",
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add_all([first, second])
    await postgres_session.commit()

    async def create_old(account: FinanceAccount, target: str) -> UUID:
        result = await create_balance_adjustment(
            postgres_session,
            owner_id=owner.id,
            ledger_id=ledger.id,
            account_id=account.id,
            transaction_date=date(2026, 8, 1),
            expected_derived_balance=Money.parse(amount="0.00", currency="CNY"),
            expected_account_nature="asset",
            target_balance=Money.parse(amount=target, currency="CNY"),
            note=None,
            project=lambda item: item,
        )
        assert result.transaction is not None
        return result.transaction.transaction.id

    first_id = await create_old(first, "1.00")
    second_id = await create_old(second, "2.00")
    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)

    async def replace(
        session: AsyncSession,
        *,
        transaction_id: UUID,
        account_id: UUID,
        expected: str,
        target: str,
    ) -> object:
        return await replace_balance_adjustment(
            session,
            owner_id=owner.id,
            ledger_id=ledger.id,
            transaction_id=transaction_id,
            account_id=account_id,
            transaction_date=date(2026, 8, 1),
            expected_derived_balance=Money.parse(amount=expected, currency="CNY"),
            expected_account_nature="asset",
            target_balance=Money.parse(amount=target, currency="CNY"),
            note=None,
            project=lambda item: item,
        )

    async with session_factory() as forward, session_factory() as reverse:
        results = await asyncio.wait_for(
            asyncio.gather(
                replace(
                    forward,
                    transaction_id=first_id,
                    account_id=second.id,
                    expected="2.00",
                    target="3.00",
                ),
                replace(
                    reverse,
                    transaction_id=second_id,
                    account_id=first.id,
                    expected="1.00",
                    target="3.00",
                ),
                return_exceptions=True,
            ),
            timeout=5,
        )

    successes = [item for item in results if not isinstance(item, Exception)]
    failures = [item for item in results if isinstance(item, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], FinanceAccountBalanceChangedError)
    movements = (await postgres_session.scalars(select(FinanceAccountMovement))).all()
    assert len(movements) == 2
    assert len({movement.account_id for movement in movements}) == 1
    assert sum((movement.amount for movement in movements), Decimal(0)) == Decimal("3.00")


async def test_concurrent_replacements_of_same_adjustment_have_one_winner(
    postgres_engine: AsyncEngine,
    postgres_session: AsyncSession,
) -> None:
    owner = _user("same-adjustment-concurrency")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add(account)
    await postgres_session.commit()
    old = await create_balance_adjustment(
        postgres_session,
        owner_id=owner.id,
        ledger_id=ledger.id,
        account_id=account.id,
        transaction_date=date(2026, 8, 1),
        expected_derived_balance=Money.parse(amount="0.00", currency="CNY"),
        expected_account_nature="asset",
        target_balance=Money.parse(amount="1.00", currency="CNY"),
        note=None,
        project=lambda result: result,
    )
    assert old.transaction is not None
    old_id = old.transaction.transaction.id

    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with (
        session_factory() as blocker,
        session_factory() as update_session,
        session_factory() as remove_session,
        session_factory() as observer,
    ):
        assert await blocker.get(FinanceTransaction, old_id, with_for_update=True)
        update_pid = await update_session.scalar(select(func.pg_backend_pid()))
        remove_pid = await remove_session.scalar(select(func.pg_backend_pid()))
        assert update_pid is not None and remove_pid is not None

        async def attempt(session: AsyncSession, target: str) -> object:
            return await replace_balance_adjustment(
                session,
                owner_id=owner.id,
                ledger_id=ledger.id,
                transaction_id=old_id,
                account_id=account.id,
                transaction_date=date(2026, 8, 1),
                expected_derived_balance=Money.parse(amount="0.00", currency="CNY"),
                expected_account_nature="asset",
                target_balance=Money.parse(amount=target, currency="CNY"),
                note=None,
                project=lambda result: result,
            )

        update_task = asyncio.create_task(attempt(update_session, "2.00"))
        assert await _wait_for_postgres_backend_lock(observer, backend_pid=update_pid)
        remove_task = asyncio.create_task(attempt(remove_session, "0.00"))
        assert await _wait_for_postgres_backend_lock(observer, backend_pid=remove_pid)
        await blocker.commit()
        results = await asyncio.gather(update_task, remove_task, return_exceptions=True)

    failures = [item for item in results if isinstance(item, Exception)]
    successes = [item for item in results if not isinstance(item, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], FinanceTransactionNotFoundError)
    winner = successes[0]
    assert isinstance(winner, BalanceAdjustmentResult)
    expected_count = 1 if winner.outcome == "updated" else 0
    assert (
        await postgres_session.scalar(select(func.count()).select_from(FinanceTransaction))
        == expected_count
    )
    assert (
        await postgres_session.scalar(select(func.count()).select_from(FinanceAccountMovement))
        == expected_count
    )
    if winner.outcome == "updated":
        assert winner.transaction is not None
        assert winner.transaction.transaction.id == old_id
        assert winner.transaction.movement.amount == Decimal("2.00")
    else:
        assert winner.outcome == "removed"
        assert winner.transaction is None


async def test_balance_adjustment_no_change_does_not_lock_semantics_but_created_history_does(
    postgres_session: AsyncSession,
) -> None:
    owner = _user("adjustment-semantics-lock")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add(account)
    await postgres_session.commit()

    no_change = await create_balance_adjustment(
        postgres_session,
        owner_id=owner.id,
        ledger_id=ledger.id,
        account_id=account.id,
        transaction_date=account.tracking_start_date,
        expected_derived_balance=Money.parse(amount="0.00", currency="CNY"),
        expected_account_nature="asset",
        target_balance=Money.parse(amount="0.00", currency="CNY"),
        note="not stored",
        project=lambda result: result,
    )
    assert no_change.outcome == "noChange"
    await correct_finance_account_semantics(
        postgres_session,
        owner_id=owner.id,
        ledger_id=ledger.id,
        account_id=account.id,
        nature="liability",
        currency=None,
    )
    created = await create_balance_adjustment(
        postgres_session,
        owner_id=owner.id,
        ledger_id=ledger.id,
        account_id=account.id,
        transaction_date=account.tracking_start_date,
        expected_derived_balance=Money.parse(amount="0.00", currency="CNY"),
        expected_account_nature="liability",
        target_balance=Money.parse(amount="1.00", currency="CNY"),
        note=None,
        project=lambda result: result,
    )
    assert created.outcome == "created"
    with pytest.raises(FinanceAccountSemanticsLockedError):
        await correct_finance_account_semantics(
            postgres_session,
            owner_id=owner.id,
            ledger_id=ledger.id,
            account_id=account.id,
            nature="asset",
            currency=None,
        )


@pytest.mark.parametrize(
    ("role", "currency", "add_allocation"),
    [
        ("primary", "CNY", False),
        ("adjustment", "USD", False),
        ("adjustment", "CNY", True),
    ],
)
async def test_database_rejects_malformed_balance_adjustment_aggregates(
    postgres_session: AsyncSession,
    role: str,
    currency: str,
    add_allocation: bool,
) -> None:
    owner = _user(uuid4().hex)
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add(account)
    await postgres_session.flush()
    transaction = FinanceTransaction(
        ledger_id=ledger.id,
        kind="balance_adjustment",
        transaction_date=account.tracking_start_date,
        note=None,
    )
    postgres_session.add(transaction)
    await postgres_session.flush()
    postgres_session.add(
        FinanceAccountMovement(
            transaction_id=transaction.id,
            ledger_id=ledger.id,
            account_id=account.id,
            amount=Decimal("1.00"),
            currency=currency,
            role=role,
        )
    )
    if add_allocation:
        postgres_session.add(
            FinanceCategoryAllocation(
                transaction_id=transaction.id,
                ledger_id=ledger.id,
                category_id=None,
                amount=Decimal("1.00"),
                currency="CNY",
            )
        )

    with pytest.raises(IntegrityError):
        await postgres_session.commit()


async def _wait_for_postgres_backend_lock(
    observer_session: AsyncSession,
    *,
    backend_pid: int,
) -> bool:
    """Wait briefly for one PostgreSQL backend to report a row-lock wait."""

    for _ in range(100):
        wait_event_type = await observer_session.scalar(
            text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :backend_pid"),
            {"backend_pid": backend_pid},
        )
        if wait_event_type == "Lock":
            return True
        await asyncio.sleep(0.01)
    return False


class FinanceLedgerSchemaInspection(TypedDict):
    """Typed subset of the reflected Finance Ledger schema."""

    tables: set[str]
    columns: list[ReflectedColumn]
    primary_key: ReflectedPrimaryKeyConstraint
    foreign_keys: list[ReflectedForeignKeyConstraint]
    unique_constraints: list[ReflectedUniqueConstraint]
    check_constraints: list[ReflectedCheckConstraint]


class FinanceAccountSchemaInspection(TypedDict):
    """Typed subset of the reflected Finance Account schema."""

    columns: list[ReflectedColumn]
    primary_key: ReflectedPrimaryKeyConstraint
    foreign_keys: list[ReflectedForeignKeyConstraint]
    check_constraints: list[ReflectedCheckConstraint]


class FinanceCategorySchemaInspection(TypedDict):
    """Typed subset of the reflected Finance Category schema."""

    columns: list[ReflectedColumn]
    primary_key: ReflectedPrimaryKeyConstraint
    foreign_keys: list[ReflectedForeignKeyConstraint]
    unique_constraints: list[ReflectedUniqueConstraint]
    check_constraints: list[ReflectedCheckConstraint]
    indexes: list[ReflectedIndex]


class FinanceTransactionSchemaInspection(TypedDict):
    """Reflected durable Transaction table shape and named invariants."""

    tables: set[str]
    transaction_columns: set[str]
    movement_columns: set[str]
    allocation_columns: set[str]
    transaction_checks: set[str | None]
    movement_checks: set[str | None]
    allocation_checks: set[str | None]
    movement_foreign_keys: set[str | None]
    allocation_foreign_keys: set[str | None]
    transaction_uniques: set[str | None]
    movement_uniques: set[str | None]
    allocation_uniques: set[str | None]
    account_uniques: set[str | None]
    category_uniques: set[str | None]
    constraint_triggers: set[str]


def inspect_finance_ledgers(sync_connection: Connection) -> FinanceLedgerSchemaInspection:
    """Return the reflected schema needed by the migration contract test."""

    schema_inspector = inspect(sync_connection)
    return {
        "tables": set(schema_inspector.get_table_names()),
        "columns": schema_inspector.get_columns("finance_ledgers"),
        "primary_key": schema_inspector.get_pk_constraint("finance_ledgers"),
        "foreign_keys": schema_inspector.get_foreign_keys("finance_ledgers"),
        "unique_constraints": schema_inspector.get_unique_constraints("finance_ledgers"),
        "check_constraints": schema_inspector.get_check_constraints("finance_ledgers"),
    }


def inspect_finance_accounts(sync_connection: Connection) -> FinanceAccountSchemaInspection:
    """Return the reflected schema needed by the Account migration test."""

    schema_inspector = inspect(sync_connection)
    return {
        "columns": schema_inspector.get_columns("finance_accounts"),
        "primary_key": schema_inspector.get_pk_constraint("finance_accounts"),
        "foreign_keys": schema_inspector.get_foreign_keys("finance_accounts"),
        "check_constraints": schema_inspector.get_check_constraints("finance_accounts"),
    }


def inspect_finance_categories(sync_connection: Connection) -> FinanceCategorySchemaInspection:
    """Return the reflected schema needed by the Category migration test."""

    schema_inspector = inspect(sync_connection)
    return {
        "columns": schema_inspector.get_columns("finance_categories"),
        "primary_key": schema_inspector.get_pk_constraint("finance_categories"),
        "foreign_keys": schema_inspector.get_foreign_keys("finance_categories"),
        "unique_constraints": schema_inspector.get_unique_constraints("finance_categories"),
        "check_constraints": schema_inspector.get_check_constraints("finance_categories"),
        "indexes": schema_inspector.get_indexes("finance_categories"),
    }


def inspect_finance_transactions(
    sync_connection: Connection,
) -> FinanceTransactionSchemaInspection:
    """Return the first durable Transaction schema and integrity constraints."""

    schema_inspector = inspect(sync_connection)
    return {
        "tables": set(schema_inspector.get_table_names()),
        "transaction_columns": {
            column["name"] for column in schema_inspector.get_columns("finance_transactions")
        },
        "movement_columns": {
            column["name"] for column in schema_inspector.get_columns("finance_account_movements")
        },
        "allocation_columns": {
            column["name"]
            for column in schema_inspector.get_columns("finance_category_allocations")
        },
        "transaction_checks": {
            constraint["name"]
            for constraint in schema_inspector.get_check_constraints("finance_transactions")
        },
        "movement_checks": {
            constraint["name"]
            for constraint in schema_inspector.get_check_constraints("finance_account_movements")
        },
        "allocation_checks": {
            constraint["name"]
            for constraint in schema_inspector.get_check_constraints("finance_category_allocations")
        },
        "movement_foreign_keys": {
            constraint["name"]
            for constraint in schema_inspector.get_foreign_keys("finance_account_movements")
        },
        "allocation_foreign_keys": {
            constraint["name"]
            for constraint in schema_inspector.get_foreign_keys("finance_category_allocations")
        },
        "transaction_uniques": {
            constraint["name"]
            for constraint in schema_inspector.get_unique_constraints("finance_transactions")
        },
        "movement_uniques": {
            constraint["name"]
            for constraint in schema_inspector.get_unique_constraints("finance_account_movements")
        },
        "allocation_uniques": {
            constraint["name"]
            for constraint in schema_inspector.get_unique_constraints(
                "finance_category_allocations"
            )
        },
        "account_uniques": {
            constraint["name"]
            for constraint in schema_inspector.get_unique_constraints("finance_accounts")
        },
        "category_uniques": {
            constraint["name"]
            for constraint in schema_inspector.get_unique_constraints("finance_categories")
        },
        "constraint_triggers": set(
            sync_connection.execute(
                text(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE NOT tgisinternal AND tgrelid IN ("
                    "'finance_accounts'::regclass, "
                    "'finance_transactions'::regclass, "
                    "'finance_account_movements'::regclass, "
                    "'finance_category_allocations'::regclass)"
                )
            ).scalars()
        ),
    }


async def test_migrations_create_one_owned_finance_ledger_schema(
    postgres_engine: AsyncEngine,
) -> None:
    alembic_config = Config(str(ALEMBIC_CONFIG_PATH))
    script_directory = ScriptDirectory.from_config(alembic_config)

    async with postgres_engine.connect() as connection:
        version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        schema = await connection.run_sync(inspect_finance_ledgers)

    assert script_directory.get_heads() == [script_directory.get_current_head()]
    assert version == script_directory.get_current_head()
    assert "finance_ledgers" in schema["tables"]
    columns = {column["name"]: column for column in schema["columns"]}
    assert set(columns) == {"id", "owner_id", "name", "name_key", "created_at", "updated_at"}
    assert all(column["nullable"] is False for column in columns.values())
    assert getattr(columns["name_key"]["type"], "collation", None) == "C"
    assert schema["primary_key"]["constrained_columns"] == ["id"]
    assert {
        (tuple(key["constrained_columns"]), key["referred_table"], tuple(key["referred_columns"]))
        for key in schema["foreign_keys"]
    } == {(("owner_id",), "users", ("id",))}
    assert {tuple(constraint["column_names"]) for constraint in schema["unique_constraints"]} == {
        ("owner_id", "name_key")
    }
    assert {constraint["name"] for constraint in schema["check_constraints"]} == {
        "ck_finance_ledgers_name_not_blank",
        "ck_finance_ledgers_name_trimmed",
        "ck_finance_ledgers_name_length",
        "ck_finance_ledgers_name_key_not_blank",
    }


async def test_migrations_add_account_state_without_a_stored_current_balance(
    postgres_engine: AsyncEngine,
) -> None:
    async with postgres_engine.connect() as connection:
        schema = await connection.run_sync(inspect_finance_accounts)

    columns = {column["name"]: column for column in schema["columns"]}
    assert set(columns) == {
        "id",
        "ledger_id",
        "name",
        "name_key",
        "nature",
        "currency",
        "opening_balance",
        "tracking_start_date",
        "status",
        "created_at",
        "updated_at",
    }
    assert all(column["nullable"] is False for column in columns.values())
    assert schema["primary_key"]["constrained_columns"] == ["id"]
    assert {
        (tuple(key["constrained_columns"]), key["referred_table"], tuple(key["referred_columns"]))
        for key in schema["foreign_keys"]
    } == {(("ledger_id",), "finance_ledgers", ("id",))}
    assert {constraint["name"] for constraint in schema["check_constraints"]} == {
        "ck_finance_accounts_name_not_blank",
        "ck_finance_accounts_name_trimmed",
        "ck_finance_accounts_name_length",
        "ck_finance_accounts_name_key_not_blank",
        "ck_finance_accounts_nature",
        "ck_finance_accounts_currency",
        "ck_finance_accounts_opening_balance_finite",
        "ck_finance_accounts_opening_balance_scale",
        "ck_finance_accounts_status",
    }


async def test_migrations_add_only_durable_ledger_owned_category_state(
    postgres_engine: AsyncEngine,
) -> None:
    async with postgres_engine.connect() as connection:
        schema = await connection.run_sync(inspect_finance_categories)

    columns = {column["name"]: column for column in schema["columns"]}
    assert set(columns) == {
        "id",
        "ledger_id",
        "name",
        "name_key",
        "status",
        "created_at",
        "updated_at",
    }
    assert all(column["nullable"] is False for column in columns.values())
    assert getattr(columns["name_key"]["type"], "collation", None) == "C"
    assert schema["primary_key"]["constrained_columns"] == ["id"]
    assert {
        (tuple(key["constrained_columns"]), key["referred_table"], tuple(key["referred_columns"]))
        for key in schema["foreign_keys"]
    } == {(("ledger_id",), "finance_ledgers", ("id",))}
    assert {tuple(constraint["column_names"]) for constraint in schema["unique_constraints"]} == {
        ("ledger_id", "name_key"),
        ("id", "ledger_id"),
    }
    assert {constraint["name"] for constraint in schema["check_constraints"]} == {
        "ck_finance_categories_name_not_blank",
        "ck_finance_categories_name_trimmed",
        "ck_finance_categories_name_length",
        "ck_finance_categories_name_key_not_blank",
        "ck_finance_categories_status",
    }
    assert {(index["name"], tuple(index["column_names"])) for index in schema["indexes"]} >= {
        (
            "ix_finance_categories_ledger_status_name_key_id",
            ("ledger_id", "status", "name_key", "id"),
        )
    }


async def test_migration_adds_atomic_transaction_movement_and_allocation_integrity(
    postgres_engine: AsyncEngine,
) -> None:
    async with postgres_engine.connect() as connection:
        schema = await connection.run_sync(inspect_finance_transactions)

    assert {
        "finance_transactions",
        "finance_account_movements",
        "finance_category_allocations",
    } <= schema["tables"]
    assert schema["transaction_columns"] == {
        "id",
        "ledger_id",
        "kind",
        "transaction_date",
        "note",
        "created_at",
        "updated_at",
    }
    assert schema["movement_columns"] == {
        "id",
        "transaction_id",
        "ledger_id",
        "account_id",
        "amount",
        "currency",
        "role",
    }
    assert schema["allocation_columns"] == {
        "id",
        "transaction_id",
        "ledger_id",
        "category_id",
        "amount",
        "currency",
    }
    assert schema["transaction_checks"] == {
        "ck_finance_transactions_kind",
        "ck_finance_transactions_note",
    }
    assert schema["movement_checks"] == {
        "ck_finance_account_movements_amount_finite",
        "ck_finance_account_movements_amount_nonzero",
        "ck_finance_account_movements_currency",
        "ck_finance_account_movements_amount_scale",
        "ck_finance_account_movements_role",
    }
    assert schema["allocation_checks"] == {
        "ck_finance_category_allocations_amount_finite",
        "ck_finance_category_allocations_amount_positive",
        "ck_finance_category_allocations_currency",
        "ck_finance_category_allocations_amount_scale",
    }
    assert schema["movement_foreign_keys"] == {
        "fk_finance_account_movements_transaction_ledger",
        "fk_finance_account_movements_account_ledger",
    }
    assert schema["allocation_foreign_keys"] == {
        "fk_finance_category_allocations_transaction_ledger",
        "fk_finance_category_allocations_category_ledger",
    }
    assert schema["transaction_uniques"] == {"uq_finance_transactions_id_ledger_id"}
    assert schema["movement_uniques"] == {"uq_finance_account_movements_transaction_role"}
    assert schema["allocation_uniques"] == {"uq_finance_category_allocations_transaction_id"}
    assert "uq_finance_accounts_id_ledger_id" in schema["account_uniques"]
    assert "uq_finance_categories_id_ledger_id" in schema["category_uniques"]
    assert schema["constraint_triggers"] == {
        "ck_finance_accounts_semantics_lock",
        "ck_finance_accounts_tracking_start_integrity",
        "ck_finance_transactions_ordinary_integrity",
        "ck_finance_account_movements_ordinary_integrity",
        "ck_finance_category_allocations_ordinary_integrity",
    }


async def test_balance_adjustment_migration_downgrades_and_upgrades_clean_database(
    postgres_engine: AsyncEngine,
) -> None:
    async with postgres_engine.begin() as connection:

        def round_trip(sync_connection: Connection) -> tuple[str, str]:
            config = Config(str(ALEMBIC_CONFIG_PATH))
            config.attributes["connection"] = sync_connection
            command.downgrade(config, "20260823_01")
            downgraded = sync_connection.scalar(text("SELECT version_num FROM alembic_version"))
            command.upgrade(config, "head")
            upgraded = sync_connection.scalar(text("SELECT version_num FROM alembic_version"))
            return str(downgraded), str(upgraded)

        downgraded, upgraded = await connection.run_sync(round_trip)

    assert downgraded == "20260823_01"
    assert upgraded == "20260825_01"


async def test_finance_ledger_requires_a_real_owner(
    postgres_session: AsyncSession,
) -> None:
    postgres_session.add(
        FinanceLedger(
            owner_id=uuid4(),
            name="Personal",
            name_key="personal",
        )
    )

    with pytest.raises(IntegrityError):
        await postgres_session.commit()


async def test_finance_ledger_name_key_is_unique_per_owner(
    postgres_session: AsyncSession,
) -> None:
    first_owner = _user("first-owner")
    second_owner = _user("second-owner")
    postgres_session.add_all([first_owner, second_owner])
    await postgres_session.flush()
    postgres_session.add_all(
        [
            _ledger(first_owner.id, name="Personal", name_key="personal"),
            _ledger(second_owner.id, name="PERSONAL", name_key="personal"),
        ]
    )
    await postgres_session.commit()

    postgres_session.add(_ledger(first_owner.id, name="PERSONAL", name_key="personal"))
    with pytest.raises(IntegrityError):
        await postgres_session.commit()


@pytest.mark.parametrize(
    ("name", "name_key"),
    (("", "empty"), (" Personal ", "personal"), ("x" * 101, "long"), ("Valid", "")),
)
async def test_finance_ledger_rejects_invalid_persisted_names(
    postgres_session: AsyncSession,
    name: str,
    name_key: str,
) -> None:
    owner = _user(uuid4().hex)
    postgres_session.add(owner)
    await postgres_session.flush()
    postgres_session.add(_ledger(owner.id, name=name, name_key=name_key))

    with pytest.raises(IntegrityError):
        await postgres_session.commit()


@pytest.mark.parametrize(
    ("nature", "currency", "opening_balance", "status"),
    (
        ("equity", "CNY", Decimal("0.00"), "active"),
        ("asset", "EUR", Decimal("0.00"), "active"),
        ("asset", "JPY", Decimal("0.1"), "active"),
        ("asset", "JPY", Decimal("100.0"), "active"),
        ("asset", "CNY", Decimal("1.000"), "active"),
        ("liability", "USD", Decimal("1.001"), "active"),
        ("asset", "CNY", Decimal("NaN"), "active"),
        ("asset", "CNY", Decimal("Infinity"), "active"),
        ("asset", "CNY", Decimal("-Infinity"), "active"),
        ("asset", "CNY", Decimal("0.00"), "deleted"),
    ),
)
async def test_finance_account_rejects_invalid_persisted_semantics(
    postgres_session: AsyncSession,
    nature: str,
    currency: str,
    opening_balance: Decimal,
    status: str,
) -> None:
    owner = _user(uuid4().hex)
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    postgres_session.add(
        _account(
            ledger.id,
            nature=nature,
            currency=currency,
            opening_balance=opening_balance,
            status=status,
        )
    )

    with pytest.raises(IntegrityError):
        await postgres_session.commit()


async def test_finance_account_accepts_supported_opening_balance_scale_boundaries(
    postgres_session: AsyncSession,
) -> None:
    owner = _user("valid-scale-boundaries")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    postgres_session.add_all(
        [
            _account(
                ledger.id,
                name="Yen",
                name_key="yen",
                nature="asset",
                currency="JPY",
                opening_balance=Decimal("100"),
                status="active",
            ),
            _account(
                ledger.id,
                name="Yuan",
                name_key="yuan",
                nature="asset",
                currency="CNY",
                opening_balance=Decimal("1.00"),
                status="active",
            ),
            _account(
                ledger.id,
                name="Dollar",
                name_key="dollar",
                nature="liability",
                currency="USD",
                opening_balance=Decimal("-1.00"),
                status="active",
            ),
        ]
    )

    await postgres_session.commit()


@pytest.mark.parametrize(
    ("name", "name_key"),
    (("", "empty"), (" Account ", "account"), ("x" * 101, "long"), ("Valid", "")),
)
async def test_finance_account_rejects_invalid_persisted_names(
    postgres_session: AsyncSession,
    name: str,
    name_key: str,
) -> None:
    owner = _user(uuid4().hex)
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    postgres_session.add(
        _account(
            ledger.id,
            name=name,
            name_key=name_key,
            nature="asset",
            currency="CNY",
            opening_balance=Decimal("0.00"),
            status="active",
        )
    )

    with pytest.raises(IntegrityError):
        await postgres_session.commit()


async def test_finance_category_name_key_is_unique_per_ledger_including_archived(
    postgres_session: AsyncSession,
) -> None:
    owner = _user("category-unique-owner")
    postgres_session.add(owner)
    await postgres_session.flush()
    first_ledger = _ledger(owner.id, name="First", name_key="first")
    second_ledger = _ledger(owner.id, name="Second", name_key="second")
    postgres_session.add_all([first_ledger, second_ledger])
    await postgres_session.flush()
    postgres_session.add_all(
        [
            _category(
                first_ledger.id,
                name="Straße",
                name_key="strasse",
                status="archived",
            ),
            _category(
                second_ledger.id,
                name="STRASSE",
                name_key="strasse",
                status="active",
            ),
        ]
    )
    await postgres_session.commit()

    postgres_session.add(
        _category(
            first_ledger.id,
            name="STRASSE",
            name_key="strasse",
            status="active",
        )
    )
    with pytest.raises(IntegrityError):
        await postgres_session.commit()


@pytest.mark.parametrize(
    ("name", "name_key", "status"),
    (
        ("", "empty", "active"),
        (" Category ", "category", "active"),
        ("x" * 101, "long", "active"),
        ("Valid", "", "active"),
        ("Valid", "valid", "deleted"),
    ),
)
async def test_finance_category_rejects_invalid_persisted_state(
    postgres_session: AsyncSession,
    name: str,
    name_key: str,
    status: str,
) -> None:
    owner = _user(uuid4().hex)
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    postgres_session.add(_category(ledger.id, name=name, name_key=name_key, status=status))

    with pytest.raises(IntegrityError):
        await postgres_session.commit()


async def test_transaction_projection_failure_rolls_back_complete_atomic_write(
    postgres_session: AsyncSession,
) -> None:
    owner = _user("transaction-projection-failure")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add(account)
    await postgres_session.commit()

    def fail_projection(_: FinanceTransactionDetail) -> None:
        raise RuntimeError("response projection failed")

    with pytest.raises(RuntimeError, match="response projection failed"):
        await create_finance_transaction(
            postgres_session,
            owner_id=owner.id,
            ledger_id=ledger.id,
            kind="income",
            account_id=account.id,
            transaction_date=date(2026, 8, 21),
            economic_amount=Money.parse(amount="10.00", currency="CNY"),
            allocation_amount=Money.parse(amount="10.00", currency="CNY"),
            category_id=None,
            note=None,
            project=fail_projection,
        )

    counts = [
        await postgres_session.scalar(select(func.count()).select_from(model))
        for model in (
            FinanceTransaction,
            FinanceAccountMovement,
            FinanceCategoryAllocation,
        )
    ]
    assert counts == [0, 0, 0]


async def test_database_rejects_an_incomplete_income_transaction(
    postgres_session: AsyncSession,
) -> None:
    owner = _user("incomplete-transaction")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    postgres_session.add(
        FinanceTransaction(
            ledger_id=ledger.id,
            kind="income",
            transaction_date=date(2026, 8, 21),
            note=None,
        )
    )

    with pytest.raises(IntegrityError):
        await postgres_session.commit()


async def test_database_rejects_incoherent_income_movement_direction(
    postgres_session: AsyncSession,
) -> None:
    owner = _user("incoherent-income")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add(account)
    await postgres_session.flush()
    transaction = FinanceTransaction(
        ledger_id=ledger.id,
        kind="income",
        transaction_date=date(2026, 8, 21),
        note=None,
    )
    postgres_session.add(transaction)
    await postgres_session.flush()
    postgres_session.add_all(
        [
            FinanceAccountMovement(
                transaction_id=transaction.id,
                ledger_id=ledger.id,
                account_id=account.id,
                amount=Decimal("-10.00"),
                currency="CNY",
            ),
            FinanceCategoryAllocation(
                transaction_id=transaction.id,
                ledger_id=ledger.id,
                category_id=None,
                amount=Decimal("10.00"),
                currency="CNY",
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        await postgres_session.commit()


async def test_database_rejects_movement_currency_that_differs_from_account(
    postgres_session: AsyncSession,
) -> None:
    owner = _user("cross-currency-movement")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add(account)
    await postgres_session.flush()
    transaction = FinanceTransaction(
        ledger_id=ledger.id,
        kind="income",
        transaction_date=date(2026, 8, 21),
        note=None,
    )
    postgres_session.add(transaction)
    await postgres_session.flush()
    postgres_session.add_all(
        [
            FinanceAccountMovement(
                transaction_id=transaction.id,
                ledger_id=ledger.id,
                account_id=account.id,
                amount=Decimal("10.00"),
                currency="USD",
            ),
            FinanceCategoryAllocation(
                transaction_id=transaction.id,
                ledger_id=ledger.id,
                category_id=None,
                amount=Decimal("10.00"),
                currency="USD",
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        await postgres_session.commit()


async def test_database_rejects_transaction_before_account_tracking_start(
    postgres_session: AsyncSession,
) -> None:
    owner = _user("transaction-before-tracking")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add(account)
    await postgres_session.flush()
    transaction = FinanceTransaction(
        ledger_id=ledger.id,
        kind="income",
        transaction_date=date(2026, 7, 31),
        note=None,
    )
    postgres_session.add(transaction)
    await postgres_session.flush()
    postgres_session.add_all(
        [
            FinanceAccountMovement(
                transaction_id=transaction.id,
                ledger_id=ledger.id,
                account_id=account.id,
                amount=Decimal("10.00"),
                currency="CNY",
            ),
            FinanceCategoryAllocation(
                transaction_id=transaction.id,
                ledger_id=ledger.id,
                category_id=None,
                amount=Decimal("10.00"),
                currency="CNY",
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        await postgres_session.commit()


async def test_database_rejects_reparenting_an_account_movement(
    postgres_session: AsyncSession,
) -> None:
    owner = _user("movement-reparenting")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add(account)
    await postgres_session.commit()

    def transaction_id(detail: FinanceTransactionDetail) -> UUID:
        return detail.transaction.id

    first_transaction_id = await create_finance_transaction(
        postgres_session,
        owner_id=owner.id,
        ledger_id=ledger.id,
        kind="income",
        account_id=account.id,
        transaction_date=date(2026, 8, 21),
        economic_amount=Money.parse(amount="10.00", currency="CNY"),
        allocation_amount=Money.parse(amount="10.00", currency="CNY"),
        category_id=None,
        note=None,
        project=transaction_id,
    )
    movement = await postgres_session.scalar(
        select(FinanceAccountMovement).where(
            FinanceAccountMovement.transaction_id == first_transaction_id
        )
    )
    assert movement is not None
    second_transaction = FinanceTransaction(
        ledger_id=ledger.id,
        kind="income",
        transaction_date=date(2026, 8, 22),
        note=None,
    )
    postgres_session.add(second_transaction)
    await postgres_session.flush()
    postgres_session.add(
        FinanceCategoryAllocation(
            transaction_id=second_transaction.id,
            ledger_id=ledger.id,
            category_id=None,
            amount=Decimal("10.00"),
            currency="CNY",
        )
    )
    movement.transaction_id = second_transaction.id

    with pytest.raises(IntegrityError):
        await postgres_session.commit()


async def test_database_rejects_tracking_start_edit_that_excludes_history(
    postgres_session: AsyncSession,
) -> None:
    owner = _user("tracking-start-persistence")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add(account)
    await postgres_session.commit()
    await create_finance_transaction(
        postgres_session,
        owner_id=owner.id,
        ledger_id=ledger.id,
        kind="income",
        account_id=account.id,
        transaction_date=date(2026, 8, 21),
        economic_amount=Money.parse(amount="10.00", currency="CNY"),
        allocation_amount=Money.parse(amount="10.00", currency="CNY"),
        category_id=None,
        note=None,
        project=lambda detail: detail.transaction.id,
    )
    account.tracking_start_date = date(2026, 8, 22)

    with pytest.raises(IntegrityError):
        await postgres_session.commit()


@pytest.mark.parametrize("semantic_change", ("nature", "currency"))
async def test_database_rejects_account_semantic_change_with_durable_history(
    postgres_session: AsyncSession,
    semantic_change: str,
) -> None:
    owner = _user(f"account-history-{semantic_change}")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add(account)
    await postgres_session.commit()
    await create_finance_transaction(
        postgres_session,
        owner_id=owner.id,
        ledger_id=ledger.id,
        kind="income",
        account_id=account.id,
        transaction_date=date(2026, 8, 21),
        economic_amount=Money.parse(amount="10.00", currency="CNY"),
        allocation_amount=Money.parse(amount="10.00", currency="CNY"),
        category_id=None,
        note=None,
        project=lambda detail: detail.transaction.id,
    )

    if semantic_change == "nature":
        account.nature = "liability"
    else:
        account.currency = "USD"

    with pytest.raises(IntegrityError):
        await postgres_session.commit()


@pytest.mark.parametrize("semantic_change", ("nature", "currency"))
async def test_database_rejects_account_semantic_change_with_opening_balance(
    postgres_session: AsyncSession,
    semantic_change: str,
) -> None:
    owner = _user(f"account-opening-{semantic_change}")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("25.00"),
        status="active",
    )
    postgres_session.add(account)
    await postgres_session.commit()

    if semantic_change == "nature":
        account.nature = "liability"
    else:
        account.currency = "USD"

    with pytest.raises(IntegrityError):
        await postgres_session.commit()


async def test_database_allows_account_semantic_change_while_unlocked(
    postgres_session: AsyncSession,
) -> None:
    owner = _user("account-semantics-unlocked")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add(account)
    await postgres_session.commit()

    account.nature = "liability"
    account.currency = "USD"
    await postgres_session.commit()
    await postgres_session.refresh(account)

    assert (account.nature, account.currency) == ("liability", "USD")


async def test_database_serializes_transaction_commit_with_tracking_start_change(
    postgres_engine: AsyncEngine,
    postgres_session: AsyncSession,
) -> None:
    owner = _user("tracking-start-concurrency")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add(account)
    await postgres_session.commit()

    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with (
        session_factory() as transaction_session,
        session_factory() as account_session,
        session_factory() as observer_session,
    ):
        transaction = FinanceTransaction(
            ledger_id=ledger.id,
            kind="income",
            transaction_date=date(2026, 8, 1),
            note=None,
        )
        transaction_session.add(transaction)
        await transaction_session.flush()
        transaction_session.add_all(
            [
                FinanceAccountMovement(
                    transaction_id=transaction.id,
                    ledger_id=ledger.id,
                    account_id=account.id,
                    amount=Decimal("10.00"),
                    currency="CNY",
                ),
                FinanceCategoryAllocation(
                    transaction_id=transaction.id,
                    ledger_id=ledger.id,
                    category_id=None,
                    amount=Decimal("10.00"),
                    currency="CNY",
                ),
            ]
        )
        await transaction_session.flush()
        transaction_backend_pid = await transaction_session.scalar(select(func.pg_backend_pid()))
        assert transaction_backend_pid is not None

        competing_account = await account_session.get(FinanceAccount, account.id)
        assert competing_account is not None
        competing_account.tracking_start_date = date(2026, 8, 2)
        await account_session.flush()

        transaction_commit = asyncio.create_task(transaction_session.commit())
        transaction_waited_for_account = await _wait_for_postgres_backend_lock(
            observer_session,
            backend_pid=transaction_backend_pid,
        )
        assert transaction_waited_for_account
        await account_session.commit()
        with pytest.raises(IntegrityError):
            await transaction_commit
        await transaction_session.rollback()

        persisted_account = await observer_session.get(FinanceAccount, account.id)
        persisted_transaction_count = await observer_session.scalar(
            select(func.count())
            .select_from(FinanceAccountMovement)
            .where(FinanceAccountMovement.account_id == account.id)
        )

    assert persisted_account is not None
    assert persisted_account.tracking_start_date == date(2026, 8, 2)
    assert persisted_transaction_count == 0


async def test_database_serializes_semantic_correction_after_concurrent_history_creation(
    postgres_engine: AsyncEngine,
    postgres_session: AsyncSession,
) -> None:
    owner = _user("semantic-correction-concurrency")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add(account)
    await postgres_session.commit()

    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with (
        session_factory() as transaction_session,
        session_factory() as correction_session,
        session_factory() as observer_session,
    ):
        transaction_account = await transaction_session.get(
            FinanceAccount,
            account.id,
            with_for_update=True,
        )
        assert transaction_account is not None
        transaction = FinanceTransaction(
            ledger_id=ledger.id,
            kind="income",
            transaction_date=date(2026, 8, 1),
            note=None,
        )
        transaction_session.add(transaction)
        await transaction_session.flush()
        transaction_session.add_all(
            [
                FinanceAccountMovement(
                    transaction_id=transaction.id,
                    ledger_id=ledger.id,
                    account_id=account.id,
                    amount=Decimal("10.00"),
                    currency="CNY",
                ),
                FinanceCategoryAllocation(
                    transaction_id=transaction.id,
                    ledger_id=ledger.id,
                    category_id=None,
                    amount=Decimal("10.00"),
                    currency="CNY",
                ),
            ]
        )
        await transaction_session.flush()

        correction_backend_pid = await correction_session.scalar(select(func.pg_backend_pid()))
        assert correction_backend_pid is not None
        correction_task = asyncio.create_task(
            correct_finance_account_semantics(
                correction_session,
                owner_id=owner.id,
                ledger_id=ledger.id,
                account_id=account.id,
                nature="liability",
                currency=None,
            )
        )
        correction_waited_for_account = await _wait_for_postgres_backend_lock(
            observer_session,
            backend_pid=correction_backend_pid,
        )
        assert correction_waited_for_account
        await transaction_session.commit()
        with pytest.raises(FinanceAccountSemanticsLockedError):
            await correction_task
        await correction_session.rollback()

        persisted_account = await observer_session.get(FinanceAccount, account.id)
        persisted_movement_count = await observer_session.scalar(
            select(func.count())
            .select_from(FinanceAccountMovement)
            .where(FinanceAccountMovement.account_id == account.id)
        )

    assert persisted_account is not None
    assert (persisted_account.nature, persisted_account.currency) == ("asset", "CNY")
    assert persisted_movement_count == 1


async def test_database_allows_transaction_on_tracking_start_boundary(
    postgres_session: AsyncSession,
) -> None:
    owner = _user("tracking-start-boundary")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add(account)
    await postgres_session.commit()

    transaction_id = await create_finance_transaction(
        postgres_session,
        owner_id=owner.id,
        ledger_id=ledger.id,
        kind="income",
        account_id=account.id,
        transaction_date=account.tracking_start_date,
        economic_amount=Money.parse(amount="10.00", currency="CNY"),
        allocation_amount=Money.parse(amount="10.00", currency="CNY"),
        category_id=None,
        note=None,
        project=lambda detail: detail.transaction.id,
    )

    assert await postgres_session.get(FinanceTransaction, transaction_id) is not None


def _user(subject: str) -> User:
    return User(
        identity_issuer="https://identity.example.test/finance",
        identity_subject=subject,
        status="active",
    )


def _ledger(owner_id: UUID, *, name: str, name_key: str) -> FinanceLedger:
    return FinanceLedger(owner_id=owner_id, name=name, name_key=name_key)


def _account(
    ledger_id: UUID,
    *,
    name: str = "Account",
    name_key: str = "account",
    nature: str,
    currency: str,
    opening_balance: Decimal,
    status: str,
) -> FinanceAccount:
    return FinanceAccount(
        ledger_id=ledger_id,
        name=name,
        name_key=name_key,
        nature=nature,
        currency=currency,
        opening_balance=opening_balance,
        tracking_start_date=date(2026, 8, 1),
        status=status,
    )


def _category(
    ledger_id: UUID,
    *,
    name: str,
    name_key: str,
    status: str,
) -> FinanceCategory:
    return FinanceCategory(
        ledger_id=ledger_id,
        name=name,
        name_key=name_key,
        status=status,
    )
