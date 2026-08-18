"""Dedicated queries for persisted Finance Ledgers."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance.models import FinanceLedger


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
