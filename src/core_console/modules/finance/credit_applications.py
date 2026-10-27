"""Explicit, atomic credit consumption with immutable origin-to-debt links."""

import json
from decimal import Decimal
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance.advance_models import FinanceAdvance, FinanceObligation
from core_console.modules.finance.advances import (
    _money,
    _problem,
    list_participant_obligations,
    require_advance_ledger,
)
from core_console.modules.finance.credit_application_models import (
    FinanceCreditApplicationAllocation,
)
from core_console.modules.finance.credits import _credit_origins
from core_console.modules.finance.models import (
    FinanceAccountMovement,
    FinanceParticipant,
    FinanceTransaction,
)
from core_console.modules.finance.money import subtract_money_amounts_exact as subtract
from core_console.modules.finance.queries import get_finance_account_for_update
from core_console.modules.finance.settlement_models import FinanceCreditOrigin, FinanceSettlement
from core_console.modules.finance.settlement_schemas import (
    CreditApplicationAllocationResponse,
    CreditApplicationCommandV7,
    CreditApplicationDraftV7,
    CreditApplicationPreview,
    CreditApplicationResponse,
    CreditConsumptionResponse,
)


async def preview_credit_application(
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
    draft: CreditApplicationDraftV7,
    lock: bool = False,
) -> CreditApplicationPreview:
    await require_advance_ledger(session, owner_id, ledger_id)
    if lock:
        receivables = (
            await session.scalars(
                select(FinanceObligation.receivable_account_id).where(
                    FinanceObligation.ledger_id == ledger_id,
                    FinanceObligation.participant_id == draft.participant_id,
                    FinanceObligation.id.in_(draft.selected_obligation_ids),
                )
            )
        ).all()
        origins = await _credit_origins(session, ledger_id, draft.participant_id)
        ids = set(receivables) | {
            o.liability_account_id for o in origins if o.amount.currency == draft.amount.currency
        }
        # Match ordinary Finance's Account-before-Participant lock order. A new
        # origin/debt appearing during the wait changes the token before writes.
        for id in sorted(ids):
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
        session,
        owner_id=owner_id,
        ledger_id=ledger_id,
        participant_id=draft.participant_id,
    )
    rows = [o for o in obligations if o.id in draft.selected_obligation_ids]
    origins = [
        o
        for o in await _credit_origins(session, ledger_id, draft.participant_id)
        if o.amount.currency == draft.amount.currency
    ]
    state = {
        "ledgerId": str(ledger_id),
        "intent": {k: v for k, v in draft.canonical().items() if k != "previewToken"},
        "participant": [str(participant.updated_at), participant.name, participant.status],
        "obligations": [o.model_dump(mode="json", by_alias=True) for o in rows],
        "origins": [o.model_dump(mode="json", by_alias=True) for o in origins],
    }
    token = sha256(json.dumps(state, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if lock and isinstance(draft, CreditApplicationCommandV7) and draft.preview_token != token:
        raise _problem(
            409,
            "finance_settlement_preview_stale",
            "Debt or credit state changed. Refresh the preview and confirm again.",
        )
    currency = draft.amount.currency
    if len(rows) != len(draft.selected_obligation_ids) or any(
        o.principal.currency != currency
        or o.source_date > draft.transaction_date
        or Decimal(o.remaining.amount) <= 0
        for o in rows
    ):
        raise _problem(
            422,
            "validation_error",
            "Select outstanding eligible Obligations in this Participant and currency only.",
        )
    debt = Decimal(0)
    credit = Decimal(0)
    for o in rows:
        debt = subtract(debt, subtract(Decimal(0), Decimal(o.remaining.amount)))
    for credit_origin in origins:
        credit = subtract(credit, subtract(Decimal(0), Decimal(credit_origin.available.amount)))
    amount = Decimal(draft.amount.amount)
    if amount > debt or amount > credit:
        raise _problem(
            422, "validation_error", "Application exceeds selected debt or available credit."
        )
    allocations: list[CreditApplicationAllocationResponse] = []
    consumed: list[CreditConsumptionResponse] = []
    target_left = {o.id: Decimal(o.remaining.amount) for o in rows}
    left = amount
    for origin in origins:
        used = min(left, Decimal(origin.available.amount))
        if used <= 0:
            continue
        if origin.transaction_date > draft.transaction_date:
            raise _problem(
                422,
                "validation_error",
                "Application date precedes a consumed credit Account's tracking start date.",
            )
        origin_left = used
        for o in rows:
            applied = min(origin_left, target_left[o.id])
            if applied <= 0:
                continue
            target_left[o.id] = subtract(target_left[o.id], applied)
            allocations.append(
                CreditApplicationAllocationResponse(
                    position=len(allocations) + 1,
                    obligationId=o.id,
                    advanceId=o.advance_id,
                    sourceDate=o.source_date,
                    originSettlementId=origin.settlement_id,
                    creditApplied=_money(applied, currency),
                    remainingAfter=_money(target_left[o.id], currency),
                )
            )
            origin_left = subtract(origin_left, applied)
        consumed.append(
            CreditConsumptionResponse(
                originSettlementId=origin.settlement_id,
                originTransactionId=origin.transaction_id,
                liabilityAccountId=origin.liability_account_id,
                originDate=origin.transaction_date,
                originConfirmationOrder=origin.confirmation_order,
                amount=_money(used, currency),
                availableBefore=origin.available,
                availableAfter=_money(subtract(Decimal(origin.available.amount), used), currency),
            )
        )
        left = subtract(left, used)
    return CreditApplicationPreview(
        previewToken=token,
        amount=_money(amount, currency),
        availableCreditBefore=_money(credit, currency),
        availableCreditAfter=_money(subtract(credit, amount), currency),
        outstandingBefore=_money(debt, currency),
        outstandingAfter=_money(subtract(debt, amount), currency),
        allocations=allocations,
        origins=consumed,
    )


async def execute_credit_application(
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
    command: CreditApplicationCommandV7,
) -> UUID:
    preview = await preview_credit_application(
        session,
        owner_id=owner_id,
        ledger_id=ledger_id,
        draft=command,
        lock=True,
    )
    settlement = FinanceSettlement(
        ledger_id=ledger_id,
        participant_id=command.participant_id,
        action_kind="creditApplication",
        receiving_account_id=None,
        reason=None,
        transaction_date=command.transaction_date,
        amount=Decimal(command.amount.amount),
        currency=command.amount.currency,
        note=command.note,
        selected_obligation_ids=sorted(command.selected_obligation_ids),
    )
    session.add(settlement)
    await session.flush()
    origins = {o.origin_settlement_id: o for o in preview.origins}
    available = {id: Decimal(o.available_before.amount) for id, o in origins.items()}
    for allocation in preview.allocations:
        obligation = await session.get(FinanceObligation, allocation.obligation_id)
        assert obligation is not None
        origin = origins[allocation.origin_settlement_id]
        transaction_id = uuid4()
        amount = Decimal(allocation.credit_applied.amount)
        session.add(
            FinanceTransaction(
                id=transaction_id,
                ledger_id=ledger_id,
                kind="internal_transfer",
                transaction_date=command.transaction_date,
                note=command.note,
            )
        )
        await session.flush()
        session.add_all(
            [
                FinanceAccountMovement(
                    transaction_id=transaction_id,
                    ledger_id=ledger_id,
                    account_id=obligation.receivable_account_id,
                    amount=subtract(Decimal(0), amount),
                    currency=settlement.currency,
                    role="source",
                ),
                FinanceAccountMovement(
                    transaction_id=transaction_id,
                    ledger_id=ledger_id,
                    account_id=origin.liability_account_id,
                    amount=subtract(Decimal(0), amount),
                    currency=settlement.currency,
                    role="destination",
                ),
                FinanceCreditApplicationAllocation(
                    settlement_id=settlement.id,
                    position=allocation.position,
                    ledger_id=ledger_id,
                    obligation_id=obligation.id,
                    origin_settlement_id=origin.origin_settlement_id,
                    transaction_id=transaction_id,
                    amount=amount,
                    remaining_after=Decimal(allocation.remaining_after.amount),
                    origin_available_before=available[origin.origin_settlement_id],
                ),
            ]
        )
        available[origin.origin_settlement_id] = subtract(
            available[origin.origin_settlement_id], amount
        )
        await session.flush()
    await inspect_credit_application(session, settlement)
    return settlement.id


async def inspect_credit_application(
    session: AsyncSession,
    settlement: FinanceSettlement,
) -> CreditApplicationResponse:
    rows = (
        await session.execute(
            select(
                FinanceCreditApplicationAllocation,
                FinanceObligation,
                FinanceAdvance,
                FinanceCreditOrigin,
                FinanceSettlement,
            )
            .join(
                FinanceObligation,
                FinanceObligation.id == FinanceCreditApplicationAllocation.obligation_id,
            )
            .join(FinanceAdvance, FinanceAdvance.id == FinanceObligation.advance_id)
            .join(
                FinanceCreditOrigin,
                FinanceCreditOrigin.settlement_id
                == FinanceCreditApplicationAllocation.origin_settlement_id,
            )
            .join(FinanceSettlement, FinanceSettlement.id == FinanceCreditOrigin.settlement_id)
            .where(FinanceCreditApplicationAllocation.settlement_id == settlement.id)
            .order_by(FinanceCreditApplicationAllocation.position)
        )
    ).all()
    currency = settlement.currency
    origins: dict[UUID, CreditConsumptionResponse] = {}
    allocations: list[CreditApplicationAllocationResponse] = []
    for allocation, obligation, advance, origin, receipt in rows:
        allocations.append(
            CreditApplicationAllocationResponse(
                position=allocation.position,
                obligationId=obligation.id,
                advanceId=advance.id,
                sourceDate=advance.transaction_date,
                originSettlementId=origin.settlement_id,
                creditApplied=_money(allocation.amount, currency),
                remainingAfter=_money(allocation.remaining_after, currency),
            )
        )
        if origin.settlement_id not in origins:
            origins[origin.settlement_id] = CreditConsumptionResponse(
                originSettlementId=origin.settlement_id,
                originTransactionId=origin.transaction_id,
                liabilityAccountId=origin.liability_account_id,
                originDate=receipt.transaction_date,
                originConfirmationOrder=receipt.confirmation_order,
                amount=_money(Decimal(0), currency),
                availableBefore=_money(allocation.origin_available_before, currency),
                availableAfter=_money(Decimal(0), currency),
            )
        consumption = origins[origin.settlement_id]
        consumption.amount = _money(
            subtract(Decimal(consumption.amount.amount), subtract(Decimal(0), allocation.amount)),
            currency,
        )
        consumption.available_after = _money(
            subtract(allocation.origin_available_before, allocation.amount), currency
        )
    return CreditApplicationResponse(
        id=settlement.id,
        ledgerId=settlement.ledger_id,
        participantId=settlement.participant_id,
        selectedObligationIds=settlement.selected_obligation_ids or [],
        transactionDate=settlement.transaction_date,
        amount=_money(settlement.amount, currency),
        note=settlement.note,
        confirmedAt=settlement.confirmed_at,
        confirmationOrder=settlement.confirmation_order,
        transactionIds=[a.transaction_id for a, *_ in rows],
        allocations=allocations,
        origins=list(origins.values()),
    )
