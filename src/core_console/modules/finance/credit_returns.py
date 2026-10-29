"""Preview, confirmation and immutable history for actual returned credit."""

import json
from decimal import Decimal
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance.advance_protection import require_unmanaged_accounts
from core_console.modules.finance.advances import _money, _problem, require_advance_ledger
from core_console.modules.finance.credit_return_models import FinanceCreditReturnFragment
from core_console.modules.finance.credits import _credit_origins
from core_console.modules.finance.models import (
    FinanceAccountMovement,
    FinanceParticipant,
    FinanceTransaction,
)
from core_console.modules.finance.money import subtract_money_amounts_exact as subtract
from core_console.modules.finance.queries import (
    get_finance_account_balance,
    get_finance_account_for_update,
)
from core_console.modules.finance.settlement_models import FinanceCreditOrigin, FinanceSettlement
from core_console.modules.finance.settlement_schemas import (
    CreditConsumptionResponse,
    CreditReturnCommandV8,
    CreditReturnDraftV8,
    CreditReturnPreview,
    CreditReturnResponse,
)


async def preview_credit_return(
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
    draft: CreditReturnDraftV8,
    lock: bool = False,
) -> CreditReturnPreview:
    await require_advance_ledger(session, owner_id, ledger_id)
    if lock:
        origins = await _credit_origins(session, ledger_id, draft.participant_id)
        ids = {draft.paying_account_id} | {
            o.liability_account_id for o in origins if o.amount.currency == draft.amount.currency
        }
        # Match the Account-before-Participant UUID lock order used by all
        # credit consumers. Re-read authoritative state after acquiring locks.
        for id in sorted(ids):
            await get_finance_account_for_update(session, ledger_id=ledger_id, account_id=id)
    balance = await get_finance_account_balance(
        session, ledger_id=ledger_id, account_id=draft.paying_account_id
    )
    if balance is None:
        raise _problem(404, "finance_account_not_found", "Paying Account was not found.")
    await require_unmanaged_accounts(session, [draft.paying_account_id])
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
    origins = [
        o
        for o in await _credit_origins(session, ledger_id, draft.participant_id)
        if o.amount.currency == draft.amount.currency
    ]
    state = {
        "ledgerId": str(ledger_id),
        "intent": {k: v for k, v in draft.canonical().items() if k != "previewToken"},
        "participant": [str(participant.updated_at), participant.name, participant.status],
        "account": [
            str(account.id),
            account.nature,
            account.currency,
            account.status,
            str(account.tracking_start_date),
            str(account.updated_at),
            str(balance.current_balance),
        ],
        "origins": [o.model_dump(mode="json", by_alias=True) for o in origins],
    }
    token = sha256(json.dumps(state, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if lock and isinstance(draft, CreditReturnCommandV8) and draft.preview_token != token:
        raise _problem(
            409,
            "finance_settlement_preview_stale",
            "Account or credit state changed. Refresh the preview and confirm again.",
        )
    if account.status != "active":
        raise _problem(409, "finance_account_archived", "Paying Account is archived.")
    if account.currency != draft.amount.currency:
        raise _problem(422, "validation_error", "Return and paying Account must use one currency.")
    if draft.transaction_date < account.tracking_start_date:
        raise _problem(
            422, "validation_error", "Return predates the paying Account Tracking Start Date."
        )
    currency = draft.amount.currency
    credit = Decimal(0)
    for origin in origins:
        credit = subtract(credit, subtract(Decimal(0), Decimal(origin.available.amount)))
    amount = Decimal(draft.amount.amount)
    if amount > credit:
        raise _problem(422, "validation_error", "Return exceeds available credit.")
    consumed: list[CreditConsumptionResponse] = []
    left = amount
    for origin in origins:
        used = min(left, Decimal(origin.available.amount))
        if used <= 0:
            continue
        if draft.transaction_date < origin.transaction_date:
            raise _problem(
                422,
                "validation_error",
                "Return predates a consumed credit Account's tracking start date.",
            )
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
    delta = amount if account.nature == "asset" else subtract(Decimal(0), amount)
    return CreditReturnPreview(
        previewToken=token,
        amount=_money(amount, currency),
        payingBalance=_money(balance.current_balance, currency),
        payingBalanceAfter=_money(subtract(balance.current_balance, delta), currency),
        availableCreditBefore=_money(credit, currency),
        availableCreditAfter=_money(subtract(credit, amount), currency),
        origins=consumed,
    )


async def execute_credit_return(
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
    command: CreditReturnCommandV8,
) -> UUID:
    preview = await preview_credit_return(
        session, owner_id=owner_id, ledger_id=ledger_id, draft=command, lock=True
    )
    balance = await get_finance_account_balance(
        session, ledger_id=ledger_id, account_id=command.paying_account_id
    )
    assert balance is not None
    settlement = FinanceSettlement(
        ledger_id=ledger_id,
        participant_id=command.participant_id,
        action_kind="creditReturn",
        paying_account_id=command.paying_account_id,
        receiving_account_id=None,
        reason=None,
        selected_obligation_ids=None,
        transaction_date=command.transaction_date,
        amount=Decimal(command.amount.amount),
        currency=command.amount.currency,
        note=command.note,
    )
    session.add(settlement)
    await session.flush()
    for position, origin in enumerate(preview.origins, 1):
        transaction_id = uuid4()
        amount = Decimal(origin.amount.amount)
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
                    account_id=command.paying_account_id,
                    amount=subtract(Decimal(0), amount)
                    if balance.account.nature == "asset"
                    else amount,
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
                FinanceCreditReturnFragment(
                    settlement_id=settlement.id,
                    position=position,
                    ledger_id=ledger_id,
                    origin_settlement_id=origin.origin_settlement_id,
                    transaction_id=transaction_id,
                    amount=amount,
                    origin_available_before=Decimal(origin.available_before.amount),
                ),
            ]
        )
        await session.flush()
    await inspect_credit_return(session, settlement)
    return settlement.id


async def inspect_credit_return(
    session: AsyncSession, settlement: FinanceSettlement
) -> CreditReturnResponse:
    rows = (
        await session.execute(
            select(FinanceCreditReturnFragment, FinanceCreditOrigin, FinanceSettlement)
            .join(
                FinanceCreditOrigin,
                FinanceCreditOrigin.settlement_id
                == FinanceCreditReturnFragment.origin_settlement_id,
            )
            .join(FinanceSettlement, FinanceSettlement.id == FinanceCreditOrigin.settlement_id)
            .where(FinanceCreditReturnFragment.settlement_id == settlement.id)
            .order_by(FinanceCreditReturnFragment.position)
        )
    ).all()
    assert settlement.paying_account_id is not None
    currency = settlement.currency
    return CreditReturnResponse(
        id=settlement.id,
        ledgerId=settlement.ledger_id,
        participantId=settlement.participant_id,
        payingAccountId=settlement.paying_account_id,
        transactionDate=settlement.transaction_date,
        amount=_money(settlement.amount, currency),
        note=settlement.note,
        confirmedAt=settlement.confirmed_at,
        confirmationOrder=settlement.confirmation_order,
        transactionIds=[fragment.transaction_id for fragment, _, _ in rows],
        origins=[
            CreditConsumptionResponse(
                originSettlementId=origin.settlement_id,
                originTransactionId=origin.transaction_id,
                liabilityAccountId=origin.liability_account_id,
                originDate=receipt.transaction_date,
                originConfirmationOrder=receipt.confirmation_order,
                amount=_money(fragment.amount, currency),
                availableBefore=_money(fragment.origin_available_before, currency),
                availableAfter=_money(
                    subtract(fragment.origin_available_before, fragment.amount), currency
                ),
            )
            for fragment, origin, receipt in rows
        ],
    )
