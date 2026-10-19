"""Preview and atomic execution of accumulated cash repayments."""

import json
from decimal import Decimal
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance.advance_models import FinanceAdvance, FinanceObligation
from core_console.modules.finance.advance_protection import require_unmanaged_accounts
from core_console.modules.finance.advances import (
    _money,
    _problem,
    list_participant_obligations,
    require_advance_ledger,
)
from core_console.modules.finance.models import (
    FinanceAccount,
    FinanceAccountMovement,
    FinanceParticipant,
    FinanceTransaction,
)
from core_console.modules.finance.money import subtract_money_amounts_exact as subtract
from core_console.modules.finance.queries import (
    get_finance_account_balance,
    get_finance_account_for_update,
)
from core_console.modules.finance.settlement_models import (
    FinanceSettlement,
    FinanceSettlementAllocation,
)
from core_console.modules.finance.settlement_schemas import (
    CashAllocationResponse,
    SettlementCommandV1,
    SettlementDraftV1,
    SettlementPreview,
    SettlementResponse,
)


async def preview_settlement(
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
    draft: SettlementDraftV1,
    lock: bool = False,
) -> SettlementPreview:
    await require_advance_ledger(session, owner_id, ledger_id)
    if lock:
        # Lock every potentially affected Account in the ordinary Finance UUID order,
        # before the Participant. Read balances only in subsequent statements.
        # A concurrently introduced eligible Obligation changes the token and is
        # rejected before any writes, so it cannot introduce an unlocked effect.
        receivables = (
            await session.scalars(
                select(FinanceObligation.receivable_account_id)
                .join(FinanceAdvance, FinanceAdvance.id == FinanceObligation.advance_id)
                .where(
                    FinanceObligation.ledger_id == ledger_id,
                    FinanceObligation.participant_id == draft.participant_id,
                    FinanceObligation.currency == draft.amount.currency,
                    FinanceAdvance.transaction_date <= draft.transaction_date,
                )
            )
        ).all()
        for account_id in sorted({draft.receiving_account_id, *receivables}):
            await get_finance_account_for_update(
                session, ledger_id=ledger_id, account_id=account_id
            )
    balance = await get_finance_account_balance(
        session, ledger_id=ledger_id, account_id=draft.receiving_account_id
    )
    if balance is None:
        raise _problem(404, "finance_account_not_found", "Receiving Account was not found.")
    await require_unmanaged_accounts(session, [draft.receiving_account_id])
    query = (
        select(FinanceParticipant)
        .where(
            FinanceParticipant.id == draft.participant_id, FinanceParticipant.ledger_id == ledger_id
        )
        .execution_options(populate_existing=True)
    )
    participant = await session.scalar(query.with_for_update() if lock else query)
    if participant is None:
        raise _problem(404, "finance_participant_not_found", "Participant was not found.")
    account = balance.account
    if account.status != "active":
        raise _problem(409, "finance_account_archived", "Receiving Account is archived.")
    if account.currency != draft.amount.currency:
        raise _problem(
            422, "validation_error", "Receipt and receiving Account must use one currency."
        )
    if draft.transaction_date < account.tracking_start_date:
        raise _problem(
            422, "validation_error", "Receipt predates the receiving Account Tracking Start Date."
        )
    obligations = await list_participant_obligations(
        session, owner_id=owner_id, ledger_id=ledger_id, participant_id=draft.participant_id
    )
    eligible = [
        row
        for row in obligations
        if row.source_date <= draft.transaction_date and row.principal.currency == account.currency
    ]
    total = Decimal(0)
    left = Decimal(draft.amount.amount)
    allocations: list[CashAllocationResponse] = []
    for row in eligible:
        remaining = Decimal(row.remaining.amount)
        total = subtract(total, subtract(Decimal(0), remaining))
        paid = min(left, remaining)
        if paid > 0:
            allocations.append(
                CashAllocationResponse(
                    position=len(allocations) + 1,
                    obligationId=row.id,
                    advanceId=row.advance_id,
                    sourceDate=row.source_date,
                    cashPaid=_money(paid, account.currency),
                    remainingAfter=_money(subtract(remaining, paid), account.currency),
                )
            )
            left = subtract(left, paid)
    state = {
        "ledgerId": str(ledger_id),
        "intent": SettlementDraftV1.canonical(draft),
        "receiving": [
            str(account.updated_at),
            account.name,
            account.nature,
            account.currency,
            account.status,
            account.tracking_start_date.isoformat(),
            _money(balance.current_balance, account.currency).amount,
        ],
        "participant": [str(participant.updated_at), participant.name, participant.status],
        "obligations": [row.model_dump(mode="json", by_alias=True) for row in eligible],
    }
    token = sha256(json.dumps(state, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if lock and isinstance(draft, SettlementCommandV1) and token != draft.preview_token:
        raise _problem(
            409,
            "finance_settlement_preview_stale",
            "Repayment state changed. Refresh the preview and confirm again.",
        )
    if left > 0:
        raise _problem(
            422,
            "validation_error",
            "Cash exceeds eligible outstanding debt; "
            "explicit excess handling is not supported by this command.",
        )
    delta = Decimal(draft.amount.amount)
    if account.nature == "liability":
        delta = subtract(Decimal(0), delta)
    return SettlementPreview(
        previewToken=token,
        receivingBalance=_money(balance.current_balance, account.currency),
        receivingBalanceAfter=_money(
            subtract(balance.current_balance, subtract(Decimal(0), delta)), account.currency
        ),
        amount=_money(Decimal(draft.amount.amount), account.currency),
        outstandingBefore=_money(total, account.currency),
        outstandingAfter=_money(subtract(total, Decimal(draft.amount.amount)), account.currency),
        allocations=allocations,
    )


async def execute_create_settlement(
    session: AsyncSession, *, owner_id: UUID, ledger_id: UUID, command: SettlementCommandV1
) -> UUID:
    preview = await preview_settlement(
        session, owner_id=owner_id, ledger_id=ledger_id, draft=command, lock=True
    )
    settlement = FinanceSettlement(
        ledger_id=ledger_id,
        participant_id=command.participant_id,
        receiving_account_id=command.receiving_account_id,
        transaction_date=command.transaction_date,
        amount=Decimal(command.amount.amount),
        currency=command.amount.currency,
        note=command.note,
    )
    session.add(settlement)
    await session.flush()
    receiving = await session.get(FinanceAccount, command.receiving_account_id)
    assert receiving is not None
    for allocation in preview.allocations:
        obligation = await session.get(FinanceObligation, allocation.obligation_id)
        assert obligation is not None
        transaction_id = uuid4()
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
        paid = Decimal(allocation.cash_paid.amount)
        session.add_all(
            [
                FinanceAccountMovement(
                    transaction_id=transaction_id,
                    ledger_id=ledger_id,
                    account_id=obligation.receivable_account_id,
                    amount=subtract(Decimal(0), paid),
                    currency=command.amount.currency,
                    role="source",
                ),
                FinanceAccountMovement(
                    transaction_id=transaction_id,
                    ledger_id=ledger_id,
                    account_id=receiving.id,
                    amount=paid if receiving.nature == "asset" else subtract(Decimal(0), paid),
                    currency=command.amount.currency,
                    role="destination",
                ),
                FinanceSettlementAllocation(
                    settlement_id=settlement.id,
                    ledger_id=ledger_id,
                    position=allocation.position,
                    obligation_id=obligation.id,
                    transaction_id=transaction_id,
                    cash_paid=paid,
                    remaining_after=Decimal(allocation.remaining_after.amount),
                ),
            ]
        )
    await session.flush()
    await inspect_settlement(
        session, owner_id=owner_id, ledger_id=ledger_id, settlement_id=settlement.id
    )
    return settlement.id


async def inspect_settlement(
    session: AsyncSession, *, owner_id: UUID, ledger_id: UUID, settlement_id: UUID
) -> SettlementResponse:
    await require_advance_ledger(session, owner_id, ledger_id)
    settlement = await session.scalar(
        select(FinanceSettlement).where(
            FinanceSettlement.id == settlement_id, FinanceSettlement.ledger_id == ledger_id
        )
    )
    if settlement is None:
        raise _problem(404, "finance_settlement_not_found", "Settlement was not found.")
    rows = (
        await session.execute(
            select(FinanceSettlementAllocation, FinanceObligation, FinanceAdvance)
            .join(
                FinanceObligation, FinanceObligation.id == FinanceSettlementAllocation.obligation_id
            )
            .join(FinanceAdvance, FinanceAdvance.id == FinanceObligation.advance_id)
            .where(FinanceSettlementAllocation.settlement_id == settlement_id)
            .order_by(FinanceSettlementAllocation.position)
        )
    ).all()
    return SettlementResponse(
        id=settlement.id,
        ledgerId=ledger_id,
        participantId=settlement.participant_id,
        receivingAccountId=settlement.receiving_account_id,
        transactionDate=settlement.transaction_date,
        amount=_money(settlement.amount, settlement.currency),
        note=settlement.note,
        confirmedAt=settlement.confirmed_at,
        confirmationOrder=settlement.confirmation_order,
        transactionIds=[allocation.transaction_id for allocation, _, _ in rows],
        allocations=[
            CashAllocationResponse(
                position=allocation.position,
                obligationId=obligation.id,
                advanceId=advance.id,
                sourceDate=advance.transaction_date,
                cashPaid=_money(allocation.cash_paid, settlement.currency),
                remainingAfter=_money(allocation.remaining_after, settlement.currency),
            )
            for allocation, obligation, advance in rows
        ],
    )


async def list_participant_settlements(
    session: AsyncSession, *, owner_id: UUID, ledger_id: UUID, participant_id: UUID
) -> list[SettlementResponse]:
    # Reuse the owner/Participant boundary, including archived identities.
    await list_participant_obligations(
        session, owner_id=owner_id, ledger_id=ledger_id, participant_id=participant_id
    )
    ids = (
        await session.scalars(
            select(FinanceSettlement.id)
            .where(
                FinanceSettlement.ledger_id == ledger_id,
                FinanceSettlement.participant_id == participant_id,
            )
            .order_by(FinanceSettlement.transaction_date, FinanceSettlement.confirmation_order)
        )
    ).all()
    return [
        await inspect_settlement(session, owner_id=owner_id, ledger_id=ledger_id, settlement_id=id)
        for id in ids
    ]
