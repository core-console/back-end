"""Receipt-backed credit provenance and available balances."""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance.advances import _money
from core_console.modules.finance.credit_application_models import (
    FinanceCreditApplicationAllocation,
)
from core_console.modules.finance.credit_return_models import FinanceCreditReturnFragment
from core_console.modules.finance.money import subtract_money_amounts_exact as subtract
from core_console.modules.finance.settlement_models import FinanceCreditOrigin, FinanceSettlement
from core_console.modules.finance.settlement_schemas import CreditOriginResponse


async def credit_applications_by_origin(
    session: AsyncSession, origin_ids: set[UUID]
) -> dict[UUID, list[tuple[FinanceCreditApplicationAllocation, FinanceSettlement]]]:
    if not origin_ids:
        return {}
    allocation = FinanceCreditApplicationAllocation
    rows = await session.execute(
        select(allocation, FinanceSettlement)
        .join(FinanceSettlement, FinanceSettlement.id == allocation.settlement_id)
        .where(allocation.origin_settlement_id.in_(origin_ids))
        .order_by(
            FinanceSettlement.confirmation_order.desc(),
            FinanceSettlement.id.desc(),
            allocation.position.desc(),
        )
        .execution_options(populate_existing=True)
    )
    result: dict[UUID, list[tuple[FinanceCreditApplicationAllocation, FinanceSettlement]]] = {}
    for fragment, settlement in rows:
        result.setdefault(fragment.origin_settlement_id, []).append((fragment, settlement))
    return result


def _origin_response(
    origin: FinanceCreditOrigin,
    settlement: FinanceSettlement,
    applied: Decimal,
    returned: Decimal,
) -> CreditOriginResponse:
    assert settlement.receiving_account_id is not None
    amount = origin.effective_amount if origin.effective_amount is not None else origin.amount
    return CreditOriginResponse(
        settlementId=settlement.id,
        ledgerId=settlement.ledger_id,
        participantId=settlement.participant_id,
        receivingAccountId=settlement.receiving_account_id,
        liabilityAccountId=origin.liability_account_id,
        transactionId=origin.transaction_id,
        transactionDate=settlement.transaction_date,
        confirmedAt=settlement.confirmed_at,
        confirmationOrder=settlement.confirmation_order,
        amount=_money(amount, settlement.currency),
        available=_money(subtract(subtract(amount, applied), returned), settlement.currency),
        hasReturnedCredit=True if returned else None,
    )


async def _credit_origins(
    session: AsyncSession, ledger_id: UUID, participant_id: UUID
) -> list[CreditOriginResponse]:
    rows = (
        await session.execute(
            select(FinanceCreditOrigin, FinanceSettlement)
            .join(FinanceSettlement, FinanceSettlement.id == FinanceCreditOrigin.settlement_id)
            .where(
                FinanceSettlement.ledger_id == ledger_id,
                FinanceSettlement.participant_id == participant_id,
            )
            .order_by(FinanceSettlement.confirmation_order, FinanceSettlement.id)
            .execution_options(populate_existing=True)
        )
    ).all()
    consumed = dict(
        (
            await session.execute(
                select(
                    FinanceCreditApplicationAllocation.origin_settlement_id,
                    func.sum(FinanceCreditApplicationAllocation.applied),
                )
                .where(
                    FinanceCreditApplicationAllocation.origin_settlement_id.in_(
                        [origin.settlement_id for origin, _ in rows]
                    )
                )
                .group_by(FinanceCreditApplicationAllocation.origin_settlement_id)
            )
        ).all()
    )
    returned = dict(
        (
            await session.execute(
                select(
                    FinanceCreditReturnFragment.origin_settlement_id,
                    func.sum(FinanceCreditReturnFragment.amount),
                )
                .where(
                    FinanceCreditReturnFragment.origin_settlement_id.in_(
                        [o.settlement_id for o, _ in rows]
                    )
                )
                .group_by(FinanceCreditReturnFragment.origin_settlement_id)
            )
        ).all()
    )
    return [
        _origin_response(
            origin,
            settlement,
            consumed.get(origin.settlement_id, Decimal(0)),
            returned.get(origin.settlement_id, Decimal(0)),
        )
        for origin, settlement in rows
    ]
