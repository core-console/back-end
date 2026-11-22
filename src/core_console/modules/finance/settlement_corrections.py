"""Atomically restate original-date cash and forgiveness facts."""

import json
from decimal import Decimal
from hashlib import sha256
from typing import Literal, cast
from uuid import UUID, uuid5

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance.advance_correction_models import (
    FinanceAdvanceCorrection,
    FinanceAdvanceCorrectionTransaction,
)
from core_console.modules.finance.advance_correction_schemas import (
    CorrectionAccountEffect,
    CorrectionMovement,
)
from core_console.modules.finance.advance_schemas import ObligationResponse
from core_console.modules.finance.advances import (
    _money,
    _problem,
    list_participant_obligations,
    require_advance_ledger,
)
from core_console.modules.finance.credit_application_models import (
    FinanceCreditApplicationAllocation,
)
from core_console.modules.finance.credit_return_models import FinanceCreditReturnFragment
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
from core_console.modules.finance.settlement_correction_models import (
    FinanceSettlementCorrection,
    FinanceSettlementCorrectionTransaction,
)
from core_console.modules.finance.settlement_correction_schemas import (
    CorrectionCashAllocation,
    ReceiptCorrectionFacts,
    ReceiptFinancialEffect,
    SettlementCorrectionCommandV1,
    SettlementCorrectionDraftV1,
    SettlementCorrectionDraftV2,
    SettlementCorrectionPreview,
    SettlementCorrectionResponse,
)
from core_console.modules.finance.settlement_models import (
    FinanceCreditOrigin,
    FinanceSettlement,
    FinanceSettlementAllocation,
    FinanceSettlementIncome,
)


def _add(left: Decimal, right: Decimal) -> Decimal:
    return subtract(left, subtract(Decimal(0), right))


async def _source(session: AsyncSession, ledger_id: UUID, id: UUID) -> FinanceSettlement:
    row = await session.scalar(
        select(FinanceSettlement)
        .where(FinanceSettlement.id == id, FinanceSettlement.ledger_id == ledger_id)
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise _problem(404, "finance_settlement_not_found", "Settlement was not found.")
    if row.action_kind != "standard":
        raise _problem(
            409,
            "finance_settlement_correction_ineligible",
            "Credit application and return require their own coordinated Correction.",
        )
    return row


async def _allocations(session: AsyncSession, id: UUID) -> list[FinanceSettlementAllocation]:
    return list(
        await session.scalars(
            select(FinanceSettlementAllocation)
            .where(FinanceSettlementAllocation.settlement_id == id)
            .order_by(FinanceSettlementAllocation.position)
            .execution_options(populate_existing=True)
        )
    )


async def _facts(session: AsyncSession, source: FinanceSettlement) -> ReceiptCorrectionFacts:
    origin = await session.get(FinanceCreditOrigin, source.id, populate_existing=True)
    income = await session.get(FinanceSettlementIncome, source.id, populate_existing=True)
    category_id = None
    if income is not None:
        category_id = await session.scalar(
            select(FinanceCategoryAllocation.category_id).where(
                FinanceCategoryAllocation.transaction_id == income.transaction_id
            )
        )
    allocations = [
        CorrectionCashAllocation(
            position=a.position,
            obligationId=a.obligation_id,
            originalCashPaid=_money(a.cash_paid, source.currency),
            cashPaid=_money(
                a.effective_cash_paid if a.effective_cash_paid is not None else a.cash_paid,
                source.currency,
            ),
            originalForgiven=_money(a.forgiven, source.currency)
            if a.kind == "forgiveness"
            else None,
            forgiven=_money(
                a.effective_forgiven if a.effective_forgiven is not None else a.forgiven,
                source.currency,
            )
            if a.kind == "forgiveness"
            else None,
            expenseTransactionId=a.transaction_id if a.kind == "forgiveness" else None,
        )
        for a in await _allocations(session, source.id)
    ]
    retained = (
        (origin.effective_amount if origin.effective_amount is not None else origin.amount)
        if origin
        else Decimal(0)
    )
    surplus = income.amount if income else Decimal(0)
    cash = _add(retained, surplus)
    forgiven = Decimal(0)
    for allocation in allocations:
        cash = _add(cash, Decimal(allocation.cash_paid.amount))
        if allocation.forgiven:
            forgiven = _add(forgiven, Decimal(allocation.forgiven.amount))
    return ReceiptCorrectionFacts(
        amount=_money(cash if source.reason is not None else source.amount, source.currency),
        forgivenessAmount=_money(forgiven, source.currency) if source.reason is not None else None,
        allocations=allocations,
        retainedCredit=_money(retained, source.currency),
        acceptedSurplus=_money(surplus, source.currency),
        categoryId=category_id,
    )


async def _effects(
    session: AsyncSession, source: FinanceSettlement
) -> list[ReceiptFinancialEffect]:
    ids = {
        a.transaction_id
        for a in await _allocations(session, source.id)
        if a.transaction_id is not None
    }
    for row in (
        await session.get(FinanceCreditOrigin, source.id, populate_existing=True),
        await session.get(FinanceSettlementIncome, source.id, populate_existing=True),
    ):
        if row is not None:
            ids.add(row.transaction_id)
    ids.update(
        await session.scalars(
            select(FinanceSettlementCorrectionTransaction.transaction_id)
            .join(
                FinanceSettlementCorrection,
                FinanceSettlementCorrection.id
                == FinanceSettlementCorrectionTransaction.correction_id,
            )
            .where(FinanceSettlementCorrection.settlement_id == source.id)
        )
    )
    ids.update(
        await session.scalars(
            select(FinanceAdvanceCorrectionTransaction.transaction_id).where(
                FinanceAdvanceCorrectionTransaction.settlement_id == source.id
            )
        )
    )
    result = []
    for id in sorted(ids):
        tx = await session.get(FinanceTransaction, id, populate_existing=True)
        assert tx is not None
        movements = (
            await session.scalars(
                select(FinanceAccountMovement)
                .where(FinanceAccountMovement.transaction_id == id)
                .order_by(FinanceAccountMovement.account_id)
                .execution_options(populate_existing=True)
            )
        ).all()
        allocation = await session.scalar(
            select(FinanceCategoryAllocation).where(FinanceCategoryAllocation.transaction_id == id)
        )
        result.append(
            ReceiptFinancialEffect(
                transactionId=id,
                kind=cast(
                    Literal["internalTransfer", "income", "expense"],
                    "internalTransfer" if tx.kind == "internal_transfer" else tx.kind,
                ),
                transactionDate=tx.transaction_date,
                note=tx.note,
                movements=[
                    CorrectionMovement(
                        accountId=m.account_id,
                        amount=_money(m.amount, m.currency),
                        role=cast(Literal["source", "destination", "primary"], m.role),
                    )
                    for m in movements
                ],
                income=_money(
                    allocation.amount if allocation and tx.kind == "income" else Decimal(0),
                    source.currency,
                ),
                expense=_money(allocation.amount, source.currency)
                if allocation and tx.kind == "expense"
                else None,
                categoryId=allocation.category_id if allocation else None,
            )
        )
    return result


def _transfer(
    source: FinanceSettlement,
    id: UUID,
    account_id: UUID,
    delta: Decimal,
    receiving: FinanceAccount,
    *,
    credit: bool = False,
) -> ReceiptFinancialEffect:
    # These are corrections to the original-date receipt, never new cash returns.
    # Positive cash resolves a receivable; positive retained cash creates a Liability.
    assert source.receiving_account_id is not None
    cash = delta if receiving.nature == "asset" else subtract(Decimal(0), delta)
    managed = delta if credit else subtract(Decimal(0), delta)
    return ReceiptFinancialEffect(
        transactionId=id,
        kind="internalTransfer",
        transactionDate=source.transaction_date,
        note=source.note,
        income=_money(Decimal(0), source.currency),
        categoryId=None,
        movements=sorted(
            [
                CorrectionMovement(
                    accountId=account_id,
                    amount=_money(managed, source.currency),
                    role="source" if delta > 0 else "destination",
                ),
                CorrectionMovement(
                    accountId=receiving.id,
                    amount=_money(cash, source.currency),
                    role="destination" if delta > 0 else "source",
                ),
            ],
            key=lambda m: m.account_id,
        ),
    )


def _correct_allocations(
    source: FinanceSettlement,
    before: ReceiptCorrectionFacts,
    scoped: list[ObligationResponse],
    cash: Decimal,
    forgiveness: Decimal,
) -> list[CorrectionCashAllocation]:
    zero = Decimal(0)
    currency = source.currency
    paid = {o.id: zero for o in scoped}
    forgiven = dict(paid)
    for a in before.allocations:
        if a.forgiven is None:
            paid[a.obligation_id] = Decimal(a.cash_paid.amount)
        else:
            forgiven[a.obligation_id] = Decimal(a.forgiven.amount)
    capacity = {
        o.id: _add(Decimal(o.remaining.amount), _add(paid[o.id], forgiven[o.id])) for o in scoped
    }
    total_paid = zero
    for value in paid.values():
        total_paid = _add(total_paid, value)
    if cash < total_paid:
        release = subtract(total_paid, cash)
        for a in reversed(before.allocations):
            if a.forgiven is None:
                removed = min(release, paid[a.obligation_id])
                paid[a.obligation_id] = subtract(paid[a.obligation_id], removed)
                release = subtract(release, removed)
    elif cash > Decimal(before.amount.amount):
        left = subtract(cash, Decimal(before.amount.amount))
        for o in scoped:
            extra = min(left, subtract(capacity[o.id], paid[o.id]))
            paid[o.id] = _add(paid[o.id], extra)
            left = subtract(left, extra)

    remaining = {id: subtract(value, paid[id]) for id, value in capacity.items()}
    total_forgiven = zero
    for id, value in forgiven.items():
        forgiven[id] = min(value, remaining[id])
        total_forgiven = _add(total_forgiven, forgiven[id])
    if forgiveness < total_forgiven:
        release = subtract(total_forgiven, forgiveness)
        for a in reversed(before.allocations):
            if a.forgiven is not None:
                removed = min(release, forgiven[a.obligation_id])
                forgiven[a.obligation_id] = subtract(forgiven[a.obligation_id], removed)
                release = subtract(release, removed)
    else:
        left = subtract(forgiveness, total_forgiven)
        for o in scoped:
            extra = min(left, subtract(remaining[o.id], forgiven[o.id]))
            forgiven[o.id] = _add(forgiven[o.id], extra)
            left = subtract(left, extra)
        if left:
            raise _problem(
                422,
                "validation_error",
                "Corrected forgiveness exceeds debt in the original selected scope after cash.",
            )

    result = []
    seen = set()
    for a in before.allocations:
        is_forgiveness = a.forgiven is not None
        seen.add((a.obligation_id, is_forgiveness))
        result.append(
            a.model_copy(
                update={
                    "forgiven" if is_forgiveness else "cash_paid": _money(
                        (forgiven if is_forgiveness else paid)[a.obligation_id], currency
                    )
                }
            )
        )
    # Existing positions remain immutable. New slots are confirmed by this Correction.
    for values, is_forgiveness in ((paid, False), (forgiven, True)):
        for id, value in values.items():
            if value and (id, is_forgiveness) not in seen:
                result.append(
                    CorrectionCashAllocation(
                        position=len(result) + 1,
                        obligationId=id,
                        originalCashPaid=_money(zero, currency),
                        cashPaid=_money(zero if is_forgiveness else value, currency),
                        originalForgiven=_money(zero, currency) if is_forgiveness else None,
                        forgiven=_money(value, currency) if is_forgiveness else None,
                    )
                )
    return result


async def preview_settlement_correction(
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
    draft: SettlementCorrectionDraftV1,
    lock: bool = False,
) -> SettlementCorrectionPreview:
    await require_advance_ledger(session, owner_id, ledger_id)
    source = await _source(session, ledger_id, draft.settlement_id)
    has_forgiveness = isinstance(draft, SettlementCorrectionDraftV2)
    if has_forgiveness != (source.reason is not None):
        raise _problem(
            409,
            "finance_settlement_correction_ineligible",
            "Use the Correction command for the original Settlement kind.",
        )
    rows = await _allocations(session, source.id)
    obligations = {
        o.id: o
        for o in await list_participant_obligations(
            session,
            owner_id=owner_id,
            ledger_id=ledger_id,
            participant_id=source.participant_id,
        )
    }
    origin = await session.get(FinanceCreditOrigin, source.id, populate_existing=True)
    scope = (
        set(source.selected_obligation_ids or ())
        if has_forgiveness
        else {a.obligation_id for a in rows}
    )
    if has_forgiveness and (
        not scope
        or not {a.obligation_id for a in rows} <= scope
        or any(
            id not in obligations or obligations[id].principal.currency != source.currency
            for id in scope
        )
    ):
        raise _problem(
            409,
            "finance_settlement_correction_ineligible",
            "The original selected scope cannot be established from the confirmed Settlement.",
        )
    account_ids = {obligations[id].receivable_account_id for id in scope}
    if source.receiving_account_id is not None:
        account_ids.add(source.receiving_account_id)
    if origin:
        account_ids.add(origin.liability_account_id)
    if lock:
        for id in sorted(account_ids):
            await get_finance_account_for_update(session, ledger_id=ledger_id, account_id=id)
    participant = await session.scalar(
        (
            select(FinanceParticipant)
            .where(FinanceParticipant.id == source.participant_id)
            .with_for_update()
            if lock
            else select(FinanceParticipant).where(FinanceParticipant.id == source.participant_id)
        ).execution_options(populate_existing=True)
    )
    assert participant is not None
    if lock:
        await session.scalar(
            select(FinanceSettlement.id).where(FinanceSettlement.id == source.id).with_for_update()
        )
        source = await _source(session, ledger_id, draft.settlement_id)
        origin = await session.get(FinanceCreditOrigin, source.id, populate_existing=True)
        if origin and origin.liability_account_id not in account_ids:
            raise _problem(
                409,
                "finance_settlement_correction_preview_stale",
                "Receipt credit changed. Refresh the complete preview.",
            )
        obligations = {
            o.id: o
            for o in await list_participant_obligations(
                session,
                owner_id=owner_id,
                ledger_id=ledger_id,
                participant_id=source.participant_id,
            )
        }
    if origin:
        for model in (FinanceCreditApplicationAllocation, FinanceCreditReturnFragment):
            if (
                await session.scalar(
                    select(model.settlement_id)
                    .where(model.origin_settlement_id == source.id)
                    .limit(1)
                )
                is not None
            ):
                raise _problem(
                    409,
                    "finance_settlement_correction_ineligible",
                    "Receipt credit has been applied or returned "
                    "and requires a complete lifecycle Correction.",
                )
    currency = source.currency
    if draft.amount.currency != currency:
        raise _problem(422, "validation_error", "A Correction cannot change the receipt currency.")
    balances = {}
    for id in sorted(account_ids):
        balance = await get_finance_account_balance(session, ledger_id=ledger_id, account_id=id)
        assert balance is not None
        balances[id] = balance
    receiving = (
        balances[source.receiving_account_id].account if source.receiving_account_id else None
    )
    before = await _facts(session, source)
    untouched: dict[UUID, ObligationResponse] = {}
    if source.selected_allocation_order is not None:
        scoped = [obligations[id] for id in source.selected_allocation_order]
    elif has_forgiveness:
        # The original positive Allocations prove the FIFO prefix. Any untouched
        # suffix can use its original Advance keys only when those keys stayed fixed.
        original = dict.fromkeys(
            a.obligation_id
            for a in before.allocations
            if Decimal(a.original_cash_paid.amount)
            or (a.original_forgiven and Decimal(a.original_forgiven.amount))
        )
        untouched = {id: o for id, o in obligations.items() if id in scope and id not in original}
        scoped = [*[obligations[id] for id in original], *untouched.values()]
    else:
        scoped = [obligations[a.obligation_id] for a in before.allocations]
    eligible = (
        [o for o in scoped if o.source_date <= source.transaction_date]
        if has_forgiveness
        else scoped
    )
    category = (
        await get_finance_category(
            session, ledger_id=ledger_id, category_id=draft.category_id, for_update=lock
        )
        if draft.category_id is not None
        else None
    )
    revision = (
        await session.scalar(
            select(func.coalesce(func.max(FinanceSettlementCorrection.revision), 0)).where(
                FinanceSettlementCorrection.settlement_id == source.id
            )
        )
    ) or 0
    effects_before = await _effects(session, source)
    state = {
        "ledgerId": str(ledger_id),
        "intent": SettlementCorrectionDraftV2.canonical(draft)
        if isinstance(draft, SettlementCorrectionDraftV2)
        else SettlementCorrectionDraftV1.canonical(draft),
        "revision": revision,
        "receipt": before.model_dump(mode="json", by_alias=True),
        "source": [
            str(source.id),
            str(source.participant_id),
            str(source.receiving_account_id),
            str(source.transaction_date),
            source.note,
            source.confirmation_order,
            [str(id) for id in source.selected_obligation_ids]
            if source.selected_obligation_ids
            else None,
        ],
        "obligations": [o.model_dump(mode="json", by_alias=True) for o in scoped],
        "financialEffects": [e.model_dump(mode="json", by_alias=True) for e in effects_before],
        "participant": [str(participant.updated_at), participant.name, participant.status],
        "accounts": [
            [
                str(id),
                str(b.account.updated_at),
                b.account.status,
                b.account.currency,
                b.account.nature,
                str(b.account.tracking_start_date),
                str(b.current_balance),
            ]
            for id, b in sorted(balances.items())
        ],
        "category": [str(category.updated_at), category.name, category.status]
        if category
        else None,
    }
    token = sha256(json.dumps(state, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if lock and isinstance(draft, SettlementCorrectionCommandV1) and token != draft.preview_token:
        raise _problem(
            409,
            "finance_settlement_correction_preview_stale",
            "Receipt facts or effects changed. Refresh the complete preview and confirm again.",
        )
    if has_forgiveness and not {a.obligation_id for a in before.allocations} <= {
        o.id for o in eligible
    }:
        raise _problem(
            409,
            "finance_settlement_correction_ineligible",
            "An original Allocation is outside the Settlement date eligibility.",
        )
    amount = Decimal(draft.amount.amount)
    forgiveness = (
        Decimal(draft.forgiveness_amount.amount)
        if isinstance(draft, SettlementCorrectionDraftV2)
        else Decimal(0)
    )
    if receiving is None and amount:
        raise _problem(422, "validation_error", "Standalone forgiveness has no cash receipt.")
    if amount == Decimal(before.amount.amount) and forgiveness == Decimal(
        before.forgiveness_amount.amount if before.forgiveness_amount else "0"
    ):
        raise _problem(422, "validation_error", "Enter a corrected amount for the original facts.")
    allocations_after = _correct_allocations(source, before, eligible, amount, forgiveness)
    eligible_untouched = {o.id: o for o in eligible if o.id in untouched}
    if len(eligible_untouched) > 1 and any(
        a.obligation_id in eligible_untouched for a in allocations_after[len(before.allocations) :]
    ):
        corrections = FinanceAdvanceCorrection
        revised_dates = await session.scalar(
            select(corrections.id)
            .where(
                corrections.advance_id.in_(o.advance_id for o in eligible_untouched.values()),
                corrections.plan["before"]["transactionDate"].astext
                != corrections.plan["after"]["transactionDate"].astext,
            )
            .limit(1)
        )
        if revised_dates:
            raise _problem(
                409,
                "finance_settlement_correction_ineligible",
                "The original order of selected debt without an Allocation cannot be established "
                "after an Advance date Correction.",
            )
    applied = Decimal(0)
    for a in allocations_after:
        applied = _add(applied, Decimal(a.cash_paid.amount))
    excess = subtract(amount, applied)
    if (excess > 0) == (draft.excess_disposition == "none"):
        raise _problem(
            422,
            "validation_error",
            "Choose an explicit disposition for excess cash only; "
            "no disposition is needed when all cash resolves confirmed debt.",
        )
    if draft.category_id is not None:
        if category is None:
            raise _problem(404, "finance_category_not_found", "Income Category was not found.")
        if category.status != "active":
            raise _problem(409, "finance_category_archived", "Income Category is archived.")
    after = ReceiptCorrectionFacts(
        amount=_money(amount, currency),
        forgivenessAmount=_money(forgiveness, currency) if has_forgiveness else None,
        allocations=allocations_after,
        retainedCredit=_money(
            excess if draft.excess_disposition == "retainCredit" else Decimal(0), currency
        ),
        acceptedSurplus=_money(
            excess if draft.excess_disposition == "acceptIncome" else Decimal(0), currency
        ),
        categoryId=draft.category_id,
    )
    positions = {a.position: a for a in before.allocations}
    obligations_after = {o.id: o for o in scoped}
    effects_after = list(effects_before)
    for a in allocations_after:
        prior = positions.get(a.position)
        o = obligations_after[a.obligation_id]
        if a.forgiven is not None:
            value = Decimal(a.forgiven.amount)
            delta = subtract(
                value, Decimal(prior.forgiven.amount) if prior and prior.forgiven else Decimal(0)
            )
            if not delta:
                continue
            effect_id = a.expense_transaction_id
            effects_after = [e for e in effects_after if e.transaction_id != effect_id]
            if value:
                a.expense_transaction_id = effect_id or uuid5(
                    source.id, f"correction:{revision + 1}:expense:{a.position}"
                )
                effects_after.append(
                    ReceiptFinancialEffect(
                        transactionId=a.expense_transaction_id,
                        kind="expense",
                        transactionDate=source.transaction_date,
                        note=source.reason,
                        movements=[
                            CorrectionMovement(
                                accountId=o.receivable_account_id,
                                amount=_money(subtract(Decimal(0), value), currency),
                                role="primary",
                            )
                        ],
                        income=_money(Decimal(0), currency),
                        expense=a.forgiven,
                        categoryId=None,
                    )
                )
            else:
                a.expense_transaction_id = None
            field = "forgiven"
        else:
            delta = subtract(
                Decimal(a.cash_paid.amount),
                Decimal(prior.cash_paid.amount) if prior else Decimal(0),
            )
            if not delta:
                continue
            assert receiving is not None
            effects_after.append(
                _transfer(
                    source,
                    uuid5(source.id, f"correction:{revision + 1}:cash:{a.position}"),
                    obligations[a.obligation_id].receivable_account_id,
                    delta,
                    receiving,
                )
            )
            field = "cash_paid"
        obligations_after[o.id] = o.model_copy(
            update={
                field: _money(_add(Decimal(getattr(o, field).amount), delta), currency),
                "remaining": _money(subtract(Decimal(o.remaining.amount), delta), currency),
            }
        )
    credit_delta = subtract(
        Decimal(after.retained_credit.amount), Decimal(before.retained_credit.amount)
    )
    if credit_delta:
        assert receiving is not None
        effects_after.append(
            _transfer(
                source,
                uuid5(source.id, f"correction:{revision + 1}:credit")
                if origin
                else uuid5(source.id, "correction-credit-transaction"),
                origin.liability_account_id
                if origin
                else uuid5(source.id, "correction-credit-account"),
                credit_delta,
                receiving,
                credit=True,
            )
        )
    income_row = await session.get(FinanceSettlementIncome, source.id)
    if income_row:
        effects_after = [e for e in effects_after if e.transaction_id != income_row.transaction_id]
    if Decimal(after.accepted_surplus.amount) > 0:
        assert receiving is not None
        effects_after.append(
            ReceiptFinancialEffect(
                transactionId=income_row.transaction_id
                if income_row
                else uuid5(source.id, f"correction:{revision + 1}:income"),
                kind="income",
                transactionDate=source.transaction_date,
                note=source.note,
                income=after.accepted_surplus,
                categoryId=draft.category_id,
                movements=[
                    CorrectionMovement(
                        accountId=receiving.id,
                        amount=_money(
                            Decimal(after.accepted_surplus.amount)
                            if receiving.nature == "asset"
                            else subtract(Decimal(0), Decimal(after.accepted_surplus.amount)),
                            currency,
                        ),
                        role="primary",
                    )
                ],
            )
        )
    effects_after.sort(key=lambda e: e.transaction_id)
    if (
        receiving
        and receiving.status != "active"
        and (
            amount != Decimal(before.amount.amount)
            or [e for e in effects_before if e.kind != "expense"]
            != [e for e in effects_after if e.kind != "expense"]
        )
    ):
        raise _problem(409, "finance_account_archived", "Receiving Account is archived.")
    deltas: dict[UUID, Decimal] = {}
    for effects, negative in ((effects_before, True), (effects_after, False)):
        for e in effects:
            for m in e.movements:
                value = Decimal(m.amount.amount)
                deltas[m.account_id] = _add(
                    deltas.get(m.account_id, Decimal(0)),
                    subtract(Decimal(0), value) if negative else value,
                )
    accounts = []
    for id in sorted(deltas):
        balance = balances.get(id)
        current = balance.current_balance if balance else Decimal(0)
        accounts.append(
            CorrectionAccountEffect(
                accountId=id,
                nature=cast(Literal["asset", "liability"], balance.account.nature)
                if balance
                else "liability",
                balanceBefore=_money(current, currency),
                balanceAfter=_money(_add(current, deltas[id]), currency),
            )
        )
    plan = SettlementCorrectionPreview(
        previewToken="",
        settlementId=source.id,
        participantId=source.participant_id,
        receivingAccountId=source.receiving_account_id,
        transactionDate=source.transaction_date,
        originalConfirmedAt=source.confirmed_at,
        confirmationOrder=source.confirmation_order,
        selectedObligationIds=source.selected_obligation_ids,
        reason=draft.reason,
        excessDisposition=draft.excess_disposition,
        before=before,
        after=after,
        obligationsBefore=scoped,
        obligationsAfter=list(obligations_after.values()),
        financialEffectsBefore=effects_before,
        financialEffectsAfter=effects_after,
        accountEffects=accounts,
    )
    plan.preview_token = token
    return plan


async def execute_create_settlement_correction(
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
    command: SettlementCorrectionCommandV1,
) -> UUID:
    plan = await preview_settlement_correction(
        session, owner_id=owner_id, ledger_id=ledger_id, draft=command, lock=True
    )
    if plan.preview_token != command.preview_token:
        raise _problem(
            409,
            "finance_settlement_correction_preview_stale",
            "Receipt facts or effects changed. Refresh the complete preview and confirm again.",
        )
    source = await _source(session, ledger_id, command.settlement_id)
    revision = (
        await session.scalar(
            select(func.coalesce(func.max(FinanceSettlementCorrection.revision), 0)).where(
                FinanceSettlementCorrection.settlement_id == source.id
            )
        )
    ) or 0
    record = FinanceSettlementCorrection(
        ledger_id=ledger_id,
        settlement_id=source.id,
        actor_id=owner_id,
        revision=revision + 1,
        plan=plan.model_dump(mode="json", by_alias=True),
    )
    session.add(record)
    await session.flush()
    rows = {a.position: a for a in await _allocations(session, source.id)}
    removed_expenses = []
    new_allocations = []
    remaining = {o.id: o.remaining.amount for o in plan.obligations_after}
    for a in plan.after.allocations:
        row = rows.get(a.position)
        if row is None:
            row = FinanceSettlementAllocation(
                settlement_id=source.id,
                ledger_id=ledger_id,
                position=a.position,
                obligation_id=a.obligation_id,
                kind="cash" if a.forgiven is None else "forgiveness",
                cash_paid=Decimal(0),
                forgiven=Decimal(0),
                remaining_after=Decimal(remaining[a.obligation_id]),
                transaction_id=uuid5(source.id, f"correction:{record.revision}:cash:{a.position}")
                if a.forgiven is None
                else None,
            )
            new_allocations.append(row)
        if a.forgiven is None:
            effective_cash = Decimal(a.cash_paid.amount)
            if (
                source.reason is None
                or effective_cash != row.cash_paid
                or row.effective_cash_paid is not None
            ):
                row.effective_cash_paid = effective_cash
        else:
            if row.transaction_id and a.expense_transaction_id is None:
                removed_expenses.append(row.transaction_id)
            effective = Decimal(a.forgiven.amount)
            if row.transaction_id == a.expense_transaction_id:
                if effective != row.forgiven or row.effective_forgiven is not None:
                    row.effective_forgiven = effective
            else:
                # Defer a new Expense link until its Transaction has been written.
                if a.expense_transaction_id is None:
                    row.transaction_id = None
                    row.effective_forgiven = effective
    if source.reason is None:
        source.amount = Decimal(command.amount.amount)
    elif source.receiving_account_id is not None and (
        source.effective_cash_amount is not None or Decimal(command.amount.amount) != source.amount
    ):
        source.effective_cash_amount = Decimal(command.amount.amount)
    origin = await session.get(FinanceCreditOrigin, source.id)
    new_credit_id = None
    if origin:
        origin.effective_amount = Decimal(plan.after.retained_credit.amount)
    elif Decimal(plan.after.retained_credit.amount) > 0:
        account_id = uuid5(source.id, "correction-credit-account")
        new_credit_id = uuid5(source.id, "correction-credit-transaction")
        name = f"Participant credit {source.id}"
        session.add(
            FinanceAccount(
                id=account_id,
                ledger_id=ledger_id,
                name=name,
                name_key=name.casefold(),
                nature="liability",
                currency=source.currency,
                opening_balance=Decimal(0),
                tracking_start_date=source.transaction_date,
                status="active",
            )
        )
        await session.flush()
    old_income = await session.get(FinanceSettlementIncome, source.id)
    after_ids = {e.transaction_id for e in plan.financial_effects_after}
    if old_income and old_income.transaction_id not in after_ids:
        id = old_income.transaction_id
        await session.delete(old_income)
        await session.flush()
        await session.execute(
            delete(FinanceCategoryAllocation).where(FinanceCategoryAllocation.transaction_id == id)
        )
        await session.execute(
            delete(FinanceAccountMovement).where(FinanceAccountMovement.transaction_id == id)
        )
        await session.execute(delete(FinanceTransaction).where(FinanceTransaction.id == id))
        old_income = None
    if removed_expenses:
        await session.flush()
        await session.execute(
            delete(FinanceTransaction).where(FinanceTransaction.id.in_(removed_expenses))
        )
    before = {e.transaction_id: e for e in plan.financial_effects_before}
    for effect in plan.financial_effects_after:
        if before.get(effect.transaction_id) == effect:
            continue
        tx = await session.get(FinanceTransaction, effect.transaction_id)
        if tx is None:
            session.add(
                FinanceTransaction(
                    id=effect.transaction_id,
                    ledger_id=ledger_id,
                    kind="internal_transfer" if effect.kind == "internalTransfer" else effect.kind,
                    transaction_date=effect.transaction_date,
                    note=effect.note,
                )
            )
        else:
            await session.execute(
                delete(FinanceAccountMovement).where(
                    FinanceAccountMovement.transaction_id == effect.transaction_id
                )
            )
            await session.execute(
                delete(FinanceCategoryAllocation).where(
                    FinanceCategoryAllocation.transaction_id == effect.transaction_id
                )
            )
        await session.flush()
        for m in effect.movements:
            session.add(
                FinanceAccountMovement(
                    transaction_id=effect.transaction_id,
                    ledger_id=ledger_id,
                    account_id=m.account_id,
                    amount=Decimal(m.amount.amount),
                    currency=m.amount.currency,
                    role=m.role,
                )
            )
        if effect.kind in ("income", "expense"):
            categorized = effect.expense if effect.kind == "expense" else effect.income
            assert categorized is not None
            session.add(
                FinanceCategoryAllocation(
                    transaction_id=effect.transaction_id,
                    ledger_id=ledger_id,
                    category_id=effect.category_id,
                    amount=Decimal(categorized.amount),
                    currency=source.currency,
                )
            )
        if effect.kind == "expense":
            continue
        if effect.kind == "income":
            if old_income:
                old_income.amount = Decimal(effect.income.amount)
            else:
                session.add(
                    FinanceSettlementIncome(
                        settlement_id=source.id,
                        ledger_id=ledger_id,
                        transaction_id=effect.transaction_id,
                        amount=Decimal(effect.income.amount),
                    )
                )
        elif effect.transaction_id == new_credit_id:
            session.add(
                FinanceCreditOrigin(
                    settlement_id=source.id,
                    ledger_id=ledger_id,
                    liability_account_id=uuid5(source.id, "correction-credit-account"),
                    transaction_id=effect.transaction_id,
                    amount=Decimal(plan.after.retained_credit.amount),
                )
            )
        elif all(row.transaction_id != effect.transaction_id for row in new_allocations):
            session.add(
                FinanceSettlementCorrectionTransaction(
                    transaction_id=effect.transaction_id,
                    ledger_id=ledger_id,
                    correction_id=record.id,
                )
            )
    rows.update({row.position: row for row in new_allocations})
    for a in plan.after.allocations:
        if a.forgiven is not None and a.expense_transaction_id is not None:
            row = rows[a.position]
            if row.transaction_id != a.expense_transaction_id:
                row.transaction_id = a.expense_transaction_id
                row.effective_forgiven = Decimal(a.forgiven.amount)
    session.add_all(new_allocations)
    await session.flush()
    if (
        await _facts(session, source) != plan.after
        or await _effects(session, source) != plan.financial_effects_after
    ):
        raise RuntimeError("Receipt Correction effects did not match the confirmed plan")
    await inspect_settlement_correction(
        session, owner_id=owner_id, ledger_id=ledger_id, correction_id=record.id
    )
    return record.id


def _response(row: FinanceSettlementCorrection) -> SettlementCorrectionResponse:
    return SettlementCorrectionResponse.model_validate(
        {
            **row.plan,
            "id": row.id,
            "ledgerId": row.ledger_id,
            "actorId": row.actor_id,
            "revision": row.revision,
            "confirmedAt": row.confirmed_at,
        }
    )


async def inspect_settlement_correction(
    session: AsyncSession, *, owner_id: UUID, ledger_id: UUID, correction_id: UUID
) -> SettlementCorrectionResponse:
    await require_advance_ledger(session, owner_id, ledger_id)
    row = await session.scalar(
        select(FinanceSettlementCorrection).where(
            FinanceSettlementCorrection.id == correction_id,
            FinanceSettlementCorrection.ledger_id == ledger_id,
        )
    )
    if row is None:
        raise _problem(
            404, "finance_settlement_correction_not_found", "Settlement Correction was not found."
        )
    return _response(row)


async def list_settlement_corrections(
    session: AsyncSession, *, owner_id: UUID, ledger_id: UUID, settlement_id: UUID
) -> list[SettlementCorrectionResponse]:
    await require_advance_ledger(session, owner_id, ledger_id)
    await _source(session, ledger_id, settlement_id)
    rows = await session.scalars(
        select(FinanceSettlementCorrection)
        .where(FinanceSettlementCorrection.settlement_id == settlement_id)
        .order_by(FinanceSettlementCorrection.revision)
    )
    return [_response(row) for row in rows]
