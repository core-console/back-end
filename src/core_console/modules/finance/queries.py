"""Dedicated queries for persisted Finance Ledgers."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance.models import FinanceAccount, FinanceLedger


@dataclass(frozen=True, slots=True)
class FinanceAccountBalance:
    """One Account plus its derived Current Balance read projection."""

    account: FinanceAccount
    current_balance: Decimal


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
            FinanceAccount.opening_balance.label("current_balance"),
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
) -> FinanceAccountBalance | None:
    """Return one Account balance only within its addressed Ledger."""

    statement = select(
        FinanceAccount,
        FinanceAccount.opening_balance.label("current_balance"),
    ).where(
        FinanceAccount.id == account_id,
        FinanceAccount.ledger_id == ledger_id,
    )
    result = await session.execute(statement)
    row = result.one_or_none()
    if row is None:
        return None
    account, current_balance = row
    return FinanceAccountBalance(account=account, current_balance=current_balance)
