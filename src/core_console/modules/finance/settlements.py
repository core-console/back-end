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
from core_console.modules.finance.forgiveness import (
    execute_create_forgiveness,
    preview_forgiveness,
    record_forgiveness_allocation,
)
from core_console.modules.finance.models import (
    FinanceAccount,
    FinanceAccountMovement,
    FinanceCategoryAllocation,
    FinanceParticipant,
    FinanceTransaction,
)
from core_console.modules.finance.money import subtract_money_amounts_exact as subtract
from core_console.modules.finance.queries import (
    get_finance_account_balance,
    get_finance_account_for_update,
    get_finance_category,
)
from core_console.modules.finance.settlement_models import (
    FinanceCreditOrigin,
    FinanceSettlement,
    FinanceSettlementAllocation,
    FinanceSettlementIncome,
)
from core_console.modules.finance.settlement_schemas import (
    CashAllocationResponse,
    CombinedCashAllocationResponse,
    CombinedSettlementCommandV6,
    CombinedSettlementDraftV6,
    CombinedSettlementPreview,
    CombinedSettlementResponse,
    CreditOriginResponse,
    CreditSettlementPreview,
    CreditSettlementResponse,
    ForgivenessAllocationResponse,
    ForgivenessCommandV5,
    ForgivenessDraftV5,
    ForgivenessPreview,
    ForgivenessResponse,
    IncomeSettlementPreview,
    IncomeSettlementResponse,
    ParticipantPositionResponse,
    SelectedCreditSettlementResponse,
    SelectedIncomeSettlementResponse,
    SelectedScope,
    SelectedSettlementResponse,
    SettlementCommandV1,
    SettlementCommandV2,
    SettlementCommandV3,
    SettlementCommandV4,
    SettlementDraftV1,
    SettlementDraftV2,
    SettlementDraftV3,
    SettlementDraftV4,
    SettlementPreview,
    SettlementResponse,
    SettlementSource,
)


async def preview_settlement(
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
    draft: SettlementDraftV1
    | SettlementDraftV2
    | SettlementDraftV3
    | SettlementDraftV4
    | ForgivenessDraftV5
    | CombinedSettlementDraftV6,
    lock: bool = False,
) -> (
    SettlementPreview
    | CreditSettlementPreview
    | IncomeSettlementPreview
    | ForgivenessPreview
    | CombinedSettlementPreview
):
    if isinstance(draft, ForgivenessDraftV5):
        return await preview_forgiveness(
            session, owner_id=owner_id, ledger_id=ledger_id, draft=draft, lock=lock
        )
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
    scope_invalid = False
    if isinstance(draft, SelectedScope):
        selected = set(draft.selected_obligation_ids)
        scope_invalid = not selected.issubset(
            {row.id for row in eligible if Decimal(row.remaining.amount) > 0}
        )
        eligible = [row for row in eligible if row.id in selected]
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
        "intent": {key: value for key, value in draft.canonical().items() if key != "previewToken"}
        if isinstance(draft, SelectedScope)
        else (
            SettlementDraftV3.canonical(draft)
            if isinstance(draft, SettlementDraftV3)
            else SettlementDraftV2.canonical(draft)
            if isinstance(draft, SettlementDraftV2)
            else SettlementDraftV1.canonical(draft)
        ),
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
    credit = Decimal(0)
    if isinstance(draft, (SettlementDraftV2, SettlementDraftV3)):
        origins = await _credit_origins(session, ledger_id, draft.participant_id)
        state["creditOrigins"] = [row.model_dump(mode="json", by_alias=True) for row in origins]
        for origin in origins:
            if origin.amount.currency == account.currency:
                credit = subtract(credit, subtract(Decimal(0), Decimal(origin.available.amount)))
    category = None
    if isinstance(draft, SettlementDraftV3):
        category_id = draft.category_allocations[0].category_id
        if category_id is not None:
            category = await get_finance_category(
                session, ledger_id=ledger_id, category_id=category_id, for_update=lock
            )
            if category is None:
                raise _problem(404, "finance_category_not_found", "Income Category was not found.")
            state["incomeCategory"] = [str(category.updated_at), category.name, category.status]
    token = sha256(json.dumps(state, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if lock and hasattr(draft, "preview_token") and token != draft.preview_token:
        raise _problem(
            409,
            "finance_settlement_preview_stale",
            "Repayment state changed. Refresh the preview and confirm again.",
        )
    if scope_invalid:
        raise _problem(
            422,
            "validation_error",
            "Select outstanding eligible Obligations in this Participant and currency only.",
        )
    if isinstance(draft, SettlementDraftV3):
        if category is not None and category.status != "active":
            raise _problem(409, "finance_category_archived", "Income Category is archived.")
        if left != Decimal(draft.category_allocations[0].amount.amount):
            raise _problem(
                422, "validation_error", "Income classification must equal the actual surplus."
            )
    if left > 0 and not isinstance(draft, (SettlementDraftV2, SettlementDraftV3)):
        raise _problem(
            422,
            "validation_error",
            "Cash exceeds eligible outstanding debt; "
            "explicit excess handling is not supported by this command.",
        )
    delta = Decimal(draft.amount.amount)
    if account.nature == "liability":
        delta = subtract(Decimal(0), delta)
    preview = SettlementPreview(
        previewToken=token,
        receivingBalance=_money(balance.current_balance, account.currency),
        receivingBalanceAfter=_money(
            subtract(balance.current_balance, subtract(Decimal(0), delta)), account.currency
        ),
        amount=_money(Decimal(draft.amount.amount), account.currency),
        outstandingBefore=_money(total, account.currency),
        outstandingAfter=_money(
            subtract(total, subtract(Decimal(draft.amount.amount), left)), account.currency
        ),
        allocations=allocations,
    )
    if isinstance(draft, CombinedSettlementDraftV6):
        combined_allocations: list[
            CombinedCashAllocationResponse | ForgivenessAllocationResponse
        ] = [
            CombinedCashAllocationResponse(**allocation.model_dump(by_alias=True))
            for allocation in allocations
        ]
        paid_by_obligation = {
            row.obligation_id: Decimal(row.cash_paid.amount) for row in allocations
        }
        forgiveness_left = Decimal(draft.forgiveness_amount.amount)
        for row in eligible:
            remaining = subtract(
                Decimal(row.remaining.amount), paid_by_obligation.get(row.id, Decimal(0))
            )
            forgiven = min(forgiveness_left, remaining)
            if forgiven > 0:
                combined_allocations.append(
                    ForgivenessAllocationResponse(
                        position=len(combined_allocations) + 1,
                        obligationId=row.id,
                        advanceId=row.advance_id,
                        sourceDate=row.source_date,
                        forgiven=_money(forgiven, account.currency),
                        remainingAfter=_money(subtract(remaining, forgiven), account.currency),
                    )
                )
                forgiveness_left = subtract(forgiveness_left, forgiven)
        if forgiveness_left > 0:
            raise _problem(
                422, "validation_error", "Forgiveness exceeds selected capacity after cash."
            )
        amount = _money(Decimal(draft.forgiveness_amount.amount), account.currency)
        return CombinedSettlementPreview(
            **preview.model_dump(by_alias=True, exclude={"allocations", "outstanding_after"}),
            allocations=combined_allocations,
            forgivenessAmount=amount,
            expense=amount,
            outstandingAfter=_money(
                subtract(Decimal(preview.outstanding_after.amount), Decimal(amount.amount)),
                account.currency,
            ),
        )
    if isinstance(draft, SettlementDraftV3):
        return IncomeSettlementPreview(
            **preview.model_dump(by_alias=True),
            acceptedSurplus=_money(left, account.currency),
            categoryAllocations=draft.category_allocations,
            availableCreditBefore=_money(credit, account.currency),
            availableCreditAfter=_money(credit, account.currency),
        )
    if isinstance(draft, SettlementDraftV2):
        return CreditSettlementPreview(
            **preview.model_dump(by_alias=True),
            retainedCredit=_money(left, account.currency),
            availableCreditBefore=_money(credit, account.currency),
            availableCreditAfter=_money(
                subtract(credit, subtract(Decimal(0), left)), account.currency
            ),
        )
    return preview


async def execute_create_settlement(
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
    command: SettlementCommandV1
    | SettlementCommandV2
    | SettlementCommandV3
    | SettlementCommandV4
    | ForgivenessCommandV5
    | CombinedSettlementCommandV6,
) -> UUID:
    if isinstance(command, ForgivenessCommandV5):
        return await execute_create_forgiveness(
            session, owner_id=owner_id, ledger_id=ledger_id, command=command
        )
    preview = await preview_settlement(
        session, owner_id=owner_id, ledger_id=ledger_id, draft=command, lock=True
    )
    assert not isinstance(preview, ForgivenessPreview)
    settlement = FinanceSettlement(
        ledger_id=ledger_id,
        participant_id=command.participant_id,
        receiving_account_id=command.receiving_account_id,
        transaction_date=command.transaction_date,
        amount=Decimal(command.amount.amount),
        currency=command.amount.currency,
        note=command.note,
        reason=command.reason if isinstance(command, CombinedSettlementCommandV6) else None,
        selected_obligation_ids=sorted(command.selected_obligation_ids)
        if isinstance(command, SelectedScope)
        else None,
    )
    session.add(settlement)
    await session.flush()
    receiving = await session.get(FinanceAccount, command.receiving_account_id)
    assert receiving is not None
    for allocation in preview.allocations:
        if isinstance(allocation, ForgivenessAllocationResponse):
            await record_forgiveness_allocation(session, settlement, allocation)
            continue
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
    if isinstance(preview, CreditSettlementPreview) and Decimal(preview.retained_credit.amount) > 0:
        await _record_credit(
            session, settlement, Decimal(preview.retained_credit.amount), receiving
        )
    if isinstance(command, SettlementDraftV3) and isinstance(preview, IncomeSettlementPreview):
        await _record_income(session, settlement, preview, command, receiving)
    await session.flush()
    await inspect_settlement(
        session, owner_id=owner_id, ledger_id=ledger_id, settlement_id=settlement.id
    )
    return settlement.id


async def inspect_settlement(
    session: AsyncSession, *, owner_id: UUID, ledger_id: UUID, settlement_id: UUID
) -> SettlementSource:
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
    if settlement.reason is not None and settlement.receiving_account_id is not None:
        assert settlement.selected_obligation_ids is not None
        combined_allocations: list[
            CombinedCashAllocationResponse | ForgivenessAllocationResponse
        ] = []
        forgiven = Decimal(0)
        for allocation, obligation, advance in rows:
            if allocation.kind == "forgiveness":
                forgiven = subtract(forgiven, subtract(Decimal(0), allocation.forgiven))
                combined_allocations.append(
                    ForgivenessAllocationResponse(
                        position=allocation.position,
                        obligationId=obligation.id,
                        advanceId=advance.id,
                        sourceDate=advance.transaction_date,
                        remainingAfter=_money(allocation.remaining_after, settlement.currency),
                        forgiven=_money(allocation.forgiven, settlement.currency),
                    )
                )
            else:
                combined_allocations.append(
                    CombinedCashAllocationResponse(
                        position=allocation.position,
                        obligationId=obligation.id,
                        advanceId=advance.id,
                        sourceDate=advance.transaction_date,
                        remainingAfter=_money(allocation.remaining_after, settlement.currency),
                        cashPaid=_money(allocation.cash_paid, settlement.currency),
                    )
                )
        return CombinedSettlementResponse(
            id=settlement.id,
            ledgerId=ledger_id,
            participantId=settlement.participant_id,
            receivingAccountId=settlement.receiving_account_id,
            transactionDate=settlement.transaction_date,
            amount=_money(settlement.amount, settlement.currency),
            forgivenessAmount=_money(forgiven, settlement.currency),
            reason=settlement.reason,
            note=settlement.note,
            confirmedAt=settlement.confirmed_at,
            confirmationOrder=settlement.confirmation_order,
            selectedObligationIds=settlement.selected_obligation_ids,
            transactionIds=[allocation.transaction_id for allocation, _, _ in rows],
            allocations=combined_allocations,
        )
    if settlement.reason is not None:
        assert settlement.selected_obligation_ids is not None
        return ForgivenessResponse(
            id=settlement.id,
            ledgerId=ledger_id,
            participantId=settlement.participant_id,
            transactionDate=settlement.transaction_date,
            amount=_money(settlement.amount, settlement.currency),
            reason=settlement.reason,
            note=settlement.note,
            confirmedAt=settlement.confirmed_at,
            confirmationOrder=settlement.confirmation_order,
            selectedObligationIds=settlement.selected_obligation_ids,
            transactionIds=[allocation.transaction_id for allocation, _, _ in rows],
            allocations=[
                ForgivenessAllocationResponse(
                    position=allocation.position,
                    obligationId=obligation.id,
                    advanceId=advance.id,
                    sourceDate=advance.transaction_date,
                    forgiven=_money(allocation.forgiven, settlement.currency),
                    remainingAfter=_money(allocation.remaining_after, settlement.currency),
                )
                for allocation, obligation, advance in rows
            ],
        )
    assert settlement.receiving_account_id is not None
    result = SettlementResponse(
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
    scope = (
        {"selectedObligationIds": settlement.selected_obligation_ids}
        if settlement.selected_obligation_ids
        else {}
    )
    income = await session.get(FinanceSettlementIncome, settlement.id)
    if income is not None:
        return (SelectedIncomeSettlementResponse if scope else IncomeSettlementResponse)(
            **{
                **scope,
                **result.model_dump(by_alias=True),
                "transactionIds": [*result.transaction_ids, income.transaction_id],
            },
            acceptedSurplus=_money(income.amount, settlement.currency),
            incomeTransactionId=income.transaction_id,
        )
    origin = await session.get(FinanceCreditOrigin, settlement.id)
    if origin is not None:
        return (SelectedCreditSettlementResponse if scope else CreditSettlementResponse)(
            **{
                **scope,
                **result.model_dump(by_alias=True),
                "transactionIds": [*result.transaction_ids, origin.transaction_id],
            },
            creditOrigin=_origin_response(origin, settlement),
        )
    return (
        SelectedSettlementResponse.model_validate({**result.model_dump(by_alias=True), **scope})
        if scope
        else result
    )


async def list_participant_settlements(
    session: AsyncSession, *, owner_id: UUID, ledger_id: UUID, participant_id: UUID
) -> list[SettlementSource]:
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


async def _record_credit(
    session: AsyncSession, settlement: FinanceSettlement, amount: Decimal, receiving: FinanceAccount
) -> None:
    account_id, transaction_id = uuid4(), uuid4()
    name = f"Participant credit {settlement.id}"
    session.add_all(
        [
            FinanceAccount(
                id=account_id,
                ledger_id=settlement.ledger_id,
                name=name,
                name_key=name.casefold(),
                nature="liability",
                currency=settlement.currency,
                opening_balance=Decimal(0),
                tracking_start_date=settlement.transaction_date,
                status="active",
            ),
            FinanceTransaction(
                id=transaction_id,
                ledger_id=settlement.ledger_id,
                kind="internal_transfer",
                transaction_date=settlement.transaction_date,
                note=settlement.note,
            ),
        ]
    )
    await session.flush()
    session.add_all(
        [
            FinanceAccountMovement(
                transaction_id=transaction_id,
                ledger_id=settlement.ledger_id,
                account_id=account_id,
                amount=amount,
                currency=settlement.currency,
                role="source",
            ),
            FinanceAccountMovement(
                transaction_id=transaction_id,
                ledger_id=settlement.ledger_id,
                account_id=receiving.id,
                amount=amount if receiving.nature == "asset" else subtract(Decimal(0), amount),
                currency=settlement.currency,
                role="destination",
            ),
            FinanceCreditOrigin(
                settlement_id=settlement.id,
                ledger_id=settlement.ledger_id,
                liability_account_id=account_id,
                transaction_id=transaction_id,
                amount=amount,
            ),
        ]
    )


def _origin_response(
    origin: FinanceCreditOrigin, settlement: FinanceSettlement
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
        available=_money(origin.amount, settlement.currency),
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
    return [_origin_response(origin, settlement) for origin, settlement in rows]


async def list_participant_credits(
    session: AsyncSession, *, owner_id: UUID, ledger_id: UUID, participant_id: UUID
) -> list[CreditOriginResponse]:
    await list_participant_obligations(
        session, owner_id=owner_id, ledger_id=ledger_id, participant_id=participant_id
    )
    return await _credit_origins(session, ledger_id, participant_id)


async def participant_positions(
    session: AsyncSession, *, owner_id: UUID, ledger_id: UUID, participant_id: UUID
) -> list[ParticipantPositionResponse]:
    obligations = await list_participant_obligations(
        session, owner_id=owner_id, ledger_id=ledger_id, participant_id=participant_id
    )
    origins = await _credit_origins(session, ledger_id, participant_id)
    debt: dict[str, Decimal] = {}
    credits: dict[str, Decimal] = {}
    for obligation in obligations:
        currency = obligation.remaining.currency
        debt[currency] = subtract(
            debt.get(currency, Decimal(0)),
            subtract(Decimal(0), Decimal(obligation.remaining.amount)),
        )
    for origin in origins:
        currency = origin.available.currency
        credits[currency] = subtract(
            credits.get(currency, Decimal(0)),
            subtract(Decimal(0), Decimal(origin.available.amount)),
        )
    return [
        ParticipantPositionResponse(
            currency=currency,
            outstandingReceivables=_money(debt.get(currency, Decimal(0)), currency),
            availableCredits=_money(credits.get(currency, Decimal(0)), currency),
            netExposure=_money(
                subtract(debt.get(currency, Decimal(0)), credits.get(currency, Decimal(0))),
                currency,
            ),
        )
        for currency in sorted(debt.keys() | credits.keys())
    ]


async def _record_income(
    session: AsyncSession,
    settlement: FinanceSettlement,
    preview: IncomeSettlementPreview,
    command: SettlementDraftV3,
    receiving: FinanceAccount,
) -> None:
    amount = Decimal(preview.accepted_surplus.amount)
    transaction_id = uuid4()
    session.add(
        FinanceTransaction(
            id=transaction_id,
            ledger_id=settlement.ledger_id,
            kind="income",
            transaction_date=settlement.transaction_date,
            note=settlement.note,
        )
    )
    await session.flush()
    session.add_all(
        [
            FinanceAccountMovement(
                transaction_id=transaction_id,
                ledger_id=settlement.ledger_id,
                account_id=receiving.id,
                amount=amount if receiving.nature == "asset" else subtract(Decimal(0), amount),
                currency=settlement.currency,
                role="primary",
            ),
            FinanceCategoryAllocation(
                transaction_id=transaction_id,
                ledger_id=settlement.ledger_id,
                category_id=command.category_allocations[0].category_id,
                amount=amount,
                currency=settlement.currency,
            ),
            FinanceSettlementIncome(
                settlement_id=settlement.id,
                ledger_id=settlement.ledger_id,
                transaction_id=transaction_id,
                amount=amount,
            ),
        ]
    )
