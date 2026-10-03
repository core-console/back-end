"""Caller-owned Finance create transactions on protected PostgreSQL."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from core_console.modules.finance import service
from core_console.modules.finance.models import (
    FinanceAccount,
    FinanceAccountMovement,
    FinanceCategory,
    FinanceCategoryAllocation,
    FinanceLedger,
    FinanceTransaction,
)
from core_console.modules.finance.money import Money
from core_console.modules.users.models import User

pytestmark = pytest.mark.anyio


@dataclass(frozen=True)
class CreateContext:
    owner_id: UUID
    ledger_id: UUID
    source_id: UUID
    destination_id: UUID
    category_id: UUID


@pytest.fixture
async def create_context(postgres_session: AsyncSession) -> CreateContext:
    owner = User(
        identity_issuer="https://identity.example.test",
        identity_subject="create-owner",
        status="active",
    )
    postgres_session.add(owner)
    await postgres_session.commit()
    ledger = await service.create_finance_ledger(
        postgres_session, owner_id=owner.id, name="Personal"
    )
    source = await service.create_finance_account(
        postgres_session,
        owner_id=owner.id,
        ledger_id=ledger.id,
        name="Source",
        nature="asset",
        currency="CNY",
        opening_balance=Money.parse(amount="100.00", currency="CNY"),
        tracking_start_date=date(2026, 1, 1),
    )
    destination = await service.create_finance_account(
        postgres_session,
        owner_id=owner.id,
        ledger_id=ledger.id,
        name="Destination",
        nature="liability",
        currency="CNY",
        opening_balance=Money.parse(amount="0.00", currency="CNY"),
        tracking_start_date=date(2026, 1, 1),
    )
    category = await service.create_finance_category(
        postgres_session, owner_id=owner.id, ledger_id=ledger.id, name="General"
    )
    return CreateContext(
        owner.id, ledger.id, source.account.id, destination.account.id, category.id
    )


async def _execute_create(
    session: AsyncSession,
    context: CreateContext,
    operation: str,
    *,
    fail_projection: bool = False,
) -> UUID | None:
    def project[Value](value: Value) -> Value:
        if fail_projection:
            raise RuntimeError("response projection failed")
        return value

    if operation == "ledger":
        ledger = await service.execute_create_finance_ledger(
            session, owner_id=context.owner_id, name="  Created  "
        )
        assert ledger.name == "Created"
        return ledger.id
    if operation == "account":
        balance = await service.execute_create_finance_account(
            session,
            owner_id=context.owner_id,
            ledger_id=context.ledger_id,
            name="  Created  ",
            nature="asset",
            currency="CNY",
            opening_balance=Money.parse(amount="12.34", currency="CNY"),
            tracking_start_date=date(2026, 1, 1),
        )
        assert balance.account.name == "Created"
        assert balance.current_balance == Decimal("12.34")
        return balance.account.id
    if operation == "category":
        category = await service.execute_create_finance_category(
            session, owner_id=context.owner_id, ledger_id=context.ledger_id, name="  Created  "
        )
        assert category.name == "Created"
        return category.id
    if operation in {"income", "expense"}:
        return await service.execute_create_finance_transaction(
            session,
            owner_id=context.owner_id,
            ledger_id=context.ledger_id,
            kind="income" if operation == "income" else "expense",
            account_id=context.source_id,
            transaction_date=date(2026, 8, 1),
            economic_amount=Money.parse(amount="12.34", currency="CNY"),
            allocation_amount=Money.parse(amount="12.34", currency="CNY"),
            category_id=context.category_id,
            note="  Created  ",
            project=lambda detail: project(detail).transaction.id,
        )
    if operation == "transfer":
        return await service.execute_create_internal_transfer_transaction(
            session,
            owner_id=context.owner_id,
            ledger_id=context.ledger_id,
            source_account_id=context.source_id,
            destination_account_id=context.destination_id,
            transaction_date=date(2026, 8, 1),
            amount=Money.parse(amount="12.34", currency="CNY"),
            note="  Created  ",
            project=lambda detail: project(detail).transaction.id,
        )
    if operation in {"adjustment", "noChange"}:
        result = await service.execute_create_balance_adjustment(
            session,
            owner_id=context.owner_id,
            ledger_id=context.ledger_id,
            account_id=context.source_id,
            transaction_date=date(2026, 8, 1),
            expected_derived_balance=Money.parse(amount="100.00", currency="CNY"),
            expected_account_nature="asset",
            target_balance=Money.parse(
                amount="112.34" if operation == "adjustment" else "100.00", currency="CNY"
            ),
            note="  Created  ",
            project=project,
        )
        assert result.outcome == ("created" if operation == "adjustment" else "noChange")
        if result.transaction is None:
            assert operation == "noChange"
            return None
        return result.transaction.transaction.id
    raise AssertionError(f"Unknown create: {operation}")


async def _counts(session: AsyncSession) -> list[int | None]:
    return [
        await session.scalar(select(func.count()).select_from(model))
        for model in (
            FinanceLedger,
            FinanceAccount,
            FinanceCategory,
            FinanceTransaction,
            FinanceAccountMovement,
            FinanceCategoryAllocation,
        )
    ]


@pytest.mark.parametrize(
    ("operation", "committed_counts"),
    [
        ("ledger", [3, 2, 1, 0, 0, 0]),
        ("account", [2, 3, 1, 0, 0, 0]),
        ("category", [2, 2, 2, 0, 0, 0]),
        ("income", [2, 2, 1, 1, 1, 1]),
        ("expense", [2, 2, 1, 1, 1, 1]),
        ("transfer", [2, 2, 1, 1, 2, 0]),
        ("adjustment", [2, 2, 1, 1, 1, 0]),
        ("noChange", [2, 2, 1, 0, 0, 0]),
    ],
)
@pytest.mark.parametrize("commit", [True, False], ids=["commit", "rollback"])
async def test_caller_controls_complete_create(
    postgres_engine: AsyncEngine,
    create_context: CreateContext,
    operation: str,
    committed_counts: list[int],
    commit: bool,
) -> None:
    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with session_factory() as caller, session_factory() as observer:
        transaction = await caller.begin()
        companion = FinanceLedger(
            owner_id=create_context.owner_id, name="Companion", name_key="companion"
        )
        caller.add(companion)
        created_id = await _execute_create(caller, create_context, operation)
        assert await _counts(observer) == [1, 2, 1, 0, 0, 0]
        await observer.rollback()
        if commit:
            await transaction.commit()
        else:
            await transaction.rollback()
        assert await _counts(observer) == (committed_counts if commit else [1, 2, 1, 0, 0, 0])
        balances = {
            balance.account.id: balance.current_balance
            for balance in await service.list_finance_accounts(
                observer, owner_id=create_context.owner_id, ledger_id=create_context.ledger_id
            )
        }
        expected_source = {
            "income": Decimal("112.34"),
            "expense": Decimal("87.66"),
            "transfer": Decimal("87.66"),
            "adjustment": Decimal("112.34"),
        }.get(operation, Decimal("100.00"))
        assert balances[create_context.source_id] == (
            expected_source if commit else Decimal("100.00")
        )
        assert balances[create_context.destination_id] == (
            Decimal("-12.34") if commit and operation == "transfer" else Decimal("0.00")
        )
        if commit and operation in {"income", "expense", "transfer", "adjustment"}:
            assert created_id is not None
            detail = await service.get_finance_transaction(
                observer,
                owner_id=create_context.owner_id,
                ledger_id=create_context.ledger_id,
                transaction_id=created_id,
            )
            assert detail.transaction.note == "Created"
            assert detail.transaction.transaction_date == date(2026, 8, 1)
            if operation in {"income", "expense"}:
                assert detail.allocation.amount == Decimal("12.34")
                assert detail.allocation.currency == "CNY"
                assert detail.allocation.category_id == create_context.category_id


@pytest.mark.parametrize("operation", ["income", "expense", "transfer", "adjustment", "noChange"])
async def test_caller_savepoint_rolls_back_projection_failure_without_losing_outer_write(
    postgres_engine: AsyncEngine,
    create_context: CreateContext,
    operation: str,
) -> None:
    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with session_factory() as caller, session_factory() as observer:
        async with caller.begin():
            caller.add(
                FinanceLedger(
                    owner_id=create_context.owner_id, name="Companion", name_key="companion"
                )
            )
            await caller.flush()
            with pytest.raises(RuntimeError, match="response projection failed"):
                async with caller.begin_nested():
                    await _execute_create(caller, create_context, operation, fail_projection=True)
            assert await _counts(caller) == [2, 2, 1, 0, 0, 0]
            assert await _counts(observer) == [1, 2, 1, 0, 0, 0]
        await observer.rollback()
        assert await _counts(observer) == [2, 2, 1, 0, 0, 0]


@pytest.mark.parametrize("operation", ["ledger", "category"])
async def test_caller_savepoint_recovers_translated_name_conflict(
    postgres_engine: AsyncEngine,
    create_context: CreateContext,
    operation: str,
) -> None:
    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with session_factory() as caller, session_factory() as observer:
        async with caller.begin():
            caller.add(
                FinanceLedger(
                    owner_id=create_context.owner_id, name="Companion", name_key="companion"
                )
            )
            await caller.flush()
            conflict = (
                service.FinanceLedgerNameConflictError
                if operation == "ledger"
                else service.FinanceCategoryNameConflictError
            )
            with pytest.raises(conflict):
                async with caller.begin_nested():
                    if operation == "ledger":
                        await service.execute_create_finance_ledger(
                            caller, owner_id=create_context.owner_id, name="PERSONAL"
                        )
                    else:
                        await service.execute_create_finance_category(
                            caller,
                            owner_id=create_context.owner_id,
                            ledger_id=create_context.ledger_id,
                            name="GENERAL",
                        )
            assert await _counts(caller) == [2, 2, 1, 0, 0, 0]
        assert await _counts(observer) == [2, 2, 1, 0, 0, 0]
