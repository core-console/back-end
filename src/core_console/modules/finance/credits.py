"""Receipt-backed credit provenance and available balances."""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance.advances import _money
from core_console.modules.finance.credit_application_models import (
    FinanceCreditApplicationAllocation,
)
from core_console.modules.finance.money import subtract_money_amounts_exact as subtract
from core_console.modules.finance.settlement_models import FinanceCreditOrigin, FinanceSettlement
from core_console.modules.finance.settlement_schemas import CreditOriginResponse


def _origin_response(
    origin: FinanceCreditOrigin, settlement: FinanceSettlement, consumed: Decimal = Decimal(0)
) -> CreditOriginResponse:
    assert settlement.receiving_account_id is not None
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
        amount=_money(origin.amount, settlement.currency),
        available=_money(subtract(origin.amount, consumed), settlement.currency),
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
        )
    ).all()
    consumed = dict(
        (
            await session.execute(
                select(
                    FinanceCreditApplicationAllocation.origin_settlement_id,
                    func.sum(FinanceCreditApplicationAllocation.amount),
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
    return [
        _origin_response(origin, settlement, consumed.get(origin.settlement_id, Decimal(0)))
        for origin, settlement in rows
    ]
