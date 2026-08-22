"""Dedicated queries for persisted Finance Ledgers."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import cast
from uuid import UUID

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from core_console.modules.finance.models import (
    FinanceAccount,
    FinanceAccountMovement,
    FinanceCategory,
    FinanceCategoryAllocation,
    FinanceLedger,
    FinanceTransaction,
)


@dataclass(frozen=True, slots=True)
class FinanceAccountBalance:
    """One Account plus its derived Current Balance read projection."""

    account: FinanceAccount
    current_balance: Decimal


@dataclass(frozen=True, slots=True)
class FinanceTransactionDetail:
    """Complete Income or Expense state with current embedded references."""

    transaction: FinanceTransaction
    movement: FinanceAccountMovement
    account: FinanceAccount
    allocation: FinanceCategoryAllocation
    category: FinanceCategory | None


def _current_balance_expression() -> ColumnElement[Decimal]:
    """Return the correlated durable Account balance expression."""

    movement_total = (
        select(func.coalesce(func.sum(FinanceAccountMovement.amount), Decimal(0)))
        .where(FinanceAccountMovement.account_id == FinanceAccount.id)
        .correlate(FinanceAccount)
        .scalar_subquery()
    )
    return FinanceAccount.opening_balance + movement_total


async def list_finance_ledgers(
    session: AsyncSession,
    *,
    owner_id: UUID,
) -> Sequence[FinanceLedger]:
    """Return one Local User's Ledgers in deterministic name order."""

    statement = (
        select(FinanceLedger)
        .where(FinanceLedger.owner_id == owner_id)
        .order_by(FinanceLedger.name_key, FinanceLedger.id)
    )
    result = await session.scalars(statement)
    return result.all()


async def get_finance_ledger(
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
) -> FinanceLedger | None:
    """Find one Ledger only when it belongs to the addressed Local User."""

    statement = select(FinanceLedger).where(
        FinanceLedger.id == ledger_id,
        FinanceLedger.owner_id == owner_id,
    )
    result = await session.execute(statement)
    return result.scalar_one_or_none()


async def list_finance_account_balances(
    session: AsyncSession,
    *,
    ledger_id: UUID,
) -> Sequence[FinanceAccountBalance]:
    """Return Account balance projections in deterministic lifecycle/name order."""

    statement = (
        select(
            FinanceAccount,
            _current_balance_expression().label("current_balance"),
        )
        .where(FinanceAccount.ledger_id == ledger_id)
        .order_by(
            case((FinanceAccount.status == "active", 0), else_=1),
            FinanceAccount.name_key,
            FinanceAccount.id,
        )
    )
    result = await session.execute(statement)
    return [
        FinanceAccountBalance(account=account, current_balance=current_balance)
        for account, current_balance in result.all()
    ]


async def get_finance_account_balance(
    session: AsyncSession,
    *,
    ledger_id: UUID,
    account_id: UUID,
    for_update: bool = False,
) -> FinanceAccountBalance | None:
    """Return one Account balance only within its addressed Ledger."""

    statement = select(
        FinanceAccount,
        _current_balance_expression().label("current_balance"),
    ).where(
        FinanceAccount.id == account_id,
        FinanceAccount.ledger_id == ledger_id,
    )
    if for_update:
        statement = statement.with_for_update(of=FinanceAccount)
    result = await session.execute(statement)
    row = result.one_or_none()
    if row is None:
        return None
    account, current_balance = row
    return FinanceAccountBalance(account=account, current_balance=current_balance)


async def list_finance_categories(
    session: AsyncSession,
    *,
    ledger_id: UUID,
) -> Sequence[FinanceCategory]:
    """Return Categories in deterministic lifecycle/name order."""

    statement = (
        select(FinanceCategory)
        .where(FinanceCategory.ledger_id == ledger_id)
        .order_by(
            case((FinanceCategory.status == "active", 0), else_=1),
            FinanceCategory.name_key,
            FinanceCategory.id,
        )
    )
    result = await session.scalars(statement)
    return result.all()


async def get_finance_category(
    session: AsyncSession,
    *,
    ledger_id: UUID,
    category_id: UUID,
    for_update: bool = False,
) -> FinanceCategory | None:
    """Find one Category only within its addressed Ledger."""

    statement = select(FinanceCategory).where(
        FinanceCategory.id == category_id,
        FinanceCategory.ledger_id == ledger_id,
    )
    if for_update:
        statement = statement.with_for_update(of=FinanceCategory)
    result = await session.execute(statement)
    return result.scalar_one_or_none()


async def get_finance_transaction_detail(
    session: AsyncSession,
    *,
    ledger_id: UUID,
    transaction_id: UUID,
) -> FinanceTransactionDetail | None:
    """Return one complete Transaction only within its addressed Ledger."""

    statement = (
        select(
            FinanceTransaction,
            FinanceAccountMovement,
            FinanceAccount,
            FinanceCategoryAllocation,
            FinanceCategory,
        )
        .join(
            FinanceAccountMovement,
            and_(
                FinanceAccountMovement.transaction_id == FinanceTransaction.id,
                FinanceAccountMovement.ledger_id == FinanceTransaction.ledger_id,
            ),
        )
        .join(
            FinanceAccount,
            and_(
                FinanceAccount.id == FinanceAccountMovement.account_id,
                FinanceAccount.ledger_id == FinanceAccountMovement.ledger_id,
            ),
        )
        .join(
            FinanceCategoryAllocation,
            and_(
                FinanceCategoryAllocation.transaction_id == FinanceTransaction.id,
                FinanceCategoryAllocation.ledger_id == FinanceTransaction.ledger_id,
            ),
        )
        .outerjoin(
            FinanceCategory,
            and_(
                FinanceCategory.id == FinanceCategoryAllocation.category_id,
                FinanceCategory.ledger_id == FinanceCategoryAllocation.ledger_id,
            ),
        )
        .where(
            FinanceTransaction.id == transaction_id,
            FinanceTransaction.ledger_id == ledger_id,
        )
    )
    result = await session.execute(statement)
    row = result.one_or_none()
    if row is None:
        return None
    transaction, movement, account, allocation, category = row
    return FinanceTransactionDetail(
        transaction=transaction,
        movement=movement,
        account=account,
        allocation=allocation,
        category=category,
    )


async def get_earliest_finance_account_transaction_date(
    session: AsyncSession,
    *,
    account_id: UUID,
) -> date | None:
    """Return the earliest Transaction Date affecting one Account."""

    statement = (
        select(func.min(FinanceTransaction.transaction_date))
        .join(
            FinanceAccountMovement,
            FinanceAccountMovement.transaction_id == FinanceTransaction.id,
        )
        .where(FinanceAccountMovement.account_id == account_id)
    )
    return cast(date | None, await session.scalar(statement))
