"""Selected non-cash debt resolution with one linked Expense per Allocation."""

import json
from decimal import Decimal
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance.advance_models import FinanceObligation
from core_console.modules.finance.advances import (
    _money,
    _problem,
    list_participant_obligations,
    require_advance_ledger,
)
from core_console.modules.finance.models import (
    FinanceAccountMovement,
    FinanceCategoryAllocation,
    FinanceParticipant,
    FinanceTransaction,
)
from core_console.modules.finance.money import subtract_money_amounts_exact as subtract
from core_console.modules.finance.queries import get_finance_account_for_update
from core_console.modules.finance.settlement_models import (
    FinanceSettlement,
    FinanceSettlementAllocation,
)
from core_console.modules.finance.settlement_schemas import (
    ForgivenessAllocationResponse,
    ForgivenessCommandV5,
    ForgivenessDraftV5,
    ForgivenessPreview,
)


async def preview_forgiveness(
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
    draft: ForgivenessDraftV5,
    lock: bool = False,
) -> ForgivenessPreview:
    await require_advance_ledger(session, owner_id, ledger_id)
    if lock:
        # Same Account-before-Participant lock order as cash Settlements/Advances.
        accounts = (
            await session.scalars(
                select(FinanceObligation.receivable_account_id).where(
                    FinanceObligation.ledger_id == ledger_id,
                    FinanceObligation.participant_id == draft.participant_id,
                    FinanceObligation.id.in_(draft.selected_obligation_ids),
                )
            )
        ).all()
        for id in sorted(set(accounts)):
            await get_finance_account_for_update(session, ledger_id=ledger_id, account_id=id)
    query = (
        select(FinanceParticipant)
        .where(
            FinanceParticipant.id == draft.participant_id,
            FinanceParticipant.ledger_id == ledger_id,
        )
        .execution_options(populate_existing=True)
    )
    participant = await session.scalar(query.with_for_update() if lock else query)
    if participant is None:
        raise _problem(404, "finance_participant_not_found", "Participant was not found.")
    obligations = await list_participant_obligations(
        session, owner_id=owner_id, ledger_id=ledger_id, participant_id=draft.participant_id
    )
    selected = set(draft.selected_obligation_ids)
    rows = [row for row in obligations if row.id in selected]
    state = {
        "ledgerId": str(ledger_id),
        "intent": {key: value for key, value in draft.canonical().items() if key != "previewToken"},
        "participant": [str(participant.updated_at), participant.name, participant.status],
        "obligations": [row.model_dump(mode="json", by_alias=True) for row in rows],
    }
    token = sha256(json.dumps(state, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if lock and isinstance(draft, ForgivenessCommandV5) and draft.preview_token != token:
        raise _problem(
            409,
            "finance_settlement_preview_stale",
            "Debt state changed. Refresh the preview and confirm again.",
        )
    currency = draft.amount.currency
    if len(rows) != len(selected) or any(
        row.principal.currency != currency
        or row.source_date > draft.transaction_date
        or Decimal(row.remaining.amount) <= 0
        for row in rows
    ):
        raise _problem(
            422,
            "validation_error",
            "Select outstanding eligible Obligations in this Participant and currency only.",
        )
    total = Decimal(0)
    left = Decimal(draft.amount.amount)
    allocations: list[ForgivenessAllocationResponse] = []
    for row in rows:
        remaining = Decimal(row.remaining.amount)
        total = subtract(total, subtract(Decimal(0), remaining))
        forgiven = min(left, remaining)
        if forgiven > 0:
            allocations.append(
                ForgivenessAllocationResponse(
                    position=len(allocations) + 1,
                    obligationId=row.id,
                    advanceId=row.advance_id,
                    sourceDate=row.source_date,
                    forgiven=_money(forgiven, currency),
                    remainingAfter=_money(subtract(remaining, forgiven), currency),
                )
            )
            left = subtract(left, forgiven)
    if left > 0:
        raise _problem(422, "validation_error", "Forgiveness exceeds selected outstanding debt.")
    amount = _money(Decimal(draft.amount.amount), currency)
    return ForgivenessPreview(
        previewToken=token,
        amount=amount,
        expense=amount,
        allocations=allocations,
        outstandingBefore=_money(total, currency),
        outstandingAfter=_money(subtract(total, Decimal(amount.amount)), currency),
    )


async def execute_create_forgiveness(
    session: AsyncSession, *, owner_id: UUID, ledger_id: UUID, command: ForgivenessCommandV5
) -> UUID:
    preview = await preview_forgiveness(
        session, owner_id=owner_id, ledger_id=ledger_id, draft=command, lock=True
    )
    settlement = FinanceSettlement(
        ledger_id=ledger_id,
        participant_id=command.participant_id,
        receiving_account_id=None,
        reason=command.reason,
        transaction_date=command.transaction_date,
        amount=Decimal(command.amount.amount),
        currency=command.amount.currency,
        note=command.note,
        selected_obligation_ids=sorted(command.selected_obligation_ids),
    )
    session.add(settlement)
    await session.flush()
    for allocation in preview.allocations:
        obligation = await session.get(FinanceObligation, allocation.obligation_id)
        assert obligation is not None
        transaction_id = uuid4()
        session.add(
            FinanceTransaction(
                id=transaction_id,
                ledger_id=ledger_id,
                kind="expense",
                transaction_date=command.transaction_date,
                note=command.reason,
            )
        )
        await session.flush()
        amount = Decimal(allocation.forgiven.amount)
        session.add_all(
            [
                FinanceAccountMovement(
                    transaction_id=transaction_id,
                    ledger_id=ledger_id,
                    account_id=obligation.receivable_account_id,
                    amount=subtract(Decimal(0), amount),
                    currency=command.amount.currency,
                    role="primary",
                ),
                FinanceCategoryAllocation(
                    transaction_id=transaction_id,
                    ledger_id=ledger_id,
                    category_id=None,
                    amount=amount,
                    currency=command.amount.currency,
                ),
                FinanceSettlementAllocation(
                    settlement_id=settlement.id,
                    ledger_id=ledger_id,
                    position=allocation.position,
                    obligation_id=obligation.id,
                    transaction_id=transaction_id,
                    kind="forgiveness",
                    cash_paid=Decimal(0),
                    forgiven=amount,
                    remaining_after=Decimal(allocation.remaining_after.amount),
                ),
            ]
        )
    await session.flush()
    return settlement.id
