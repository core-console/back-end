"""Keep ordinary Finance mutations outside the managed receivable boundary."""

from collections.abc import Collection
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance.advance_models import (
    FinanceAdvance,
    FinanceAdvanceTransaction,
    FinanceObligation,
)
from core_console.modules.finance.settlement_models import (
    FinanceCreditOrigin,
    FinanceSettlementAllocation,
)
from core_console.problems import ApplicationProblem


def linked_record_problem() -> ApplicationProblem:
    return ApplicationProblem(
        status=409,
        title="Conflict",
        code="finance_linked_record_protected",
        detail=(
            "This record belongs to a confirmed Advance or Settlement "
            "and requires a coordinated Correction."
        ),
    )


async def require_unmanaged_accounts(session: AsyncSession, account_ids: Collection[UUID]) -> None:
    if (
        await session.scalar(
            select(FinanceCreditOrigin.settlement_id)
            .where(FinanceCreditOrigin.liability_account_id.in_(account_ids))
            .limit(1)
        )
        is not None
    ):
        raise linked_record_problem()
    if (
        await session.scalar(
            select(FinanceObligation.id)
            .where(FinanceObligation.receivable_account_id.in_(account_ids))
            .limit(1)
        )
        is not None
    ):
        raise linked_record_problem()


async def require_unlinked_transaction(session: AsyncSession, transaction_id: UUID) -> None:
    if (
        await session.scalar(
            select(FinanceCreditOrigin.settlement_id).where(
                FinanceCreditOrigin.transaction_id == transaction_id
            )
        )
        is not None
    ):
        raise linked_record_problem()
    if (
        await session.scalar(
            select(FinanceSettlementAllocation.settlement_id).where(
                FinanceSettlementAllocation.transaction_id == transaction_id
            )
        )
        is not None
    ):
        raise linked_record_problem()
    if (
        await session.scalar(
            select(FinanceAdvance.id).where(FinanceAdvance.transaction_id == transaction_id)
        )
        is not None
    ):
        raise linked_record_problem()
    if await session.get(FinanceAdvanceTransaction, transaction_id) is not None:
        raise linked_record_problem()
