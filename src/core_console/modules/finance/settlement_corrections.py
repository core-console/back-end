"""Atomic original-date receipt restatement without replaying other Settlements."""

import json
from decimal import Decimal
from hashlib import sha256
from typing import Literal, cast
from uuid import UUID, uuid5

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance.advance_correction_schemas import (
    CorrectionAccountEffect,
    CorrectionMovement,
)
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
    if row.action_kind != "standard" or row.receiving_account_id is None or row.reason is not None:
        raise _problem(
            409,
            "finance_settlement_correction_ineligible",
            "This workflow corrects cash-only receipts. "
            "Other Settlement kinds require their coordinated workflow.",
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
    return ReceiptCorrectionFacts(
        amount=_money(source.amount, source.currency),
        allocations=[
            CorrectionCashAllocation(
                position=a.position,
                obligationId=a.obligation_id,
                originalCashPaid=_money(a.cash_paid, source.currency),
                cashPaid=_money(
                    a.effective_cash_paid if a.effective_cash_paid is not None else a.cash_paid,
                    source.currency,
                ),
            )
            for a in await _allocations(session, source.id)
        ],
        retainedCredit=_money(
            (origin.effective_amount if origin.effective_amount is not None else origin.amount)
            if origin
            else Decimal(0),
            source.currency,
        ),
        acceptedSurplus=_money(income.amount if income else Decimal(0), source.currency),
        categoryId=category_id,
    )


async def _effects(
    session: AsyncSession, source: FinanceSettlement
) -> list[ReceiptFinancialEffect]:
    ids = {a.transaction_id for a in await _allocations(session, source.id)}
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
                kind="income" if tx.kind == "income" else "internalTransfer",
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
                income=_money(allocation.amount if allocation else Decimal(0), source.currency),
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
    account_ids = {source.receiving_account_id} | {
        obligations[a.obligation_id].receivable_account_id for a in rows
    }
    if origin:
        account_ids.add(origin.liability_account_id)
    assert None not in account_ids
    if lock:
        for id in sorted(cast(set[UUID], account_ids)):
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
    for id in sorted(cast(set[UUID], account_ids)):
        balance = await get_finance_account_balance(session, ledger_id=ledger_id, account_id=id)
        assert balance is not None
        balances[id] = balance
    assert source.receiving_account_id is not None
    receiving = balances[source.receiving_account_id].account
    before = await _facts(session, source)
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
        "intent": SettlementCorrectionDraftV1.canonical(draft),
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
        "obligations": [
            obligations[a.obligation_id].model_dump(mode="json", by_alias=True)
            for a in before.allocations
        ],
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
    if receiving.status != "active":
        raise _problem(409, "finance_account_archived", "Receiving Account is archived.")
    amount = Decimal(draft.amount.amount)
    if amount == source.amount:
        raise _problem(422, "validation_error", "Enter a different original cash receipt amount.")
    paid = {a.obligation_id: Decimal(a.cash_paid.amount) for a in before.allocations}
    total_paid = Decimal(0)
    for value in paid.values():
        total_paid = _add(total_paid, value)
    if amount < total_paid:
        release = subtract(total_paid, amount)
        for a in reversed(before.allocations):
            removed = min(release, paid[a.obligation_id])
            paid[a.obligation_id] = subtract(paid[a.obligation_id], removed)
            release = subtract(release, removed)
    elif amount > source.amount:
        left = subtract(amount, total_paid)
        for a in before.allocations:
            o = obligations[a.obligation_id]
            extra = min(left, Decimal(o.remaining.amount))
            paid[o.id] = _add(paid[o.id], extra)
            left = subtract(left, extra)
    applied = Decimal(0)
    for value in paid.values():
        applied = _add(applied, value)
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
        allocations=[
            a.model_copy(update={"cash_paid": _money(paid[a.obligation_id], currency)})
            for a in before.allocations
        ],
        retainedCredit=_money(
            excess if draft.excess_disposition == "retainCredit" else Decimal(0), currency
        ),
        acceptedSurplus=_money(
            excess if draft.excess_disposition == "acceptIncome" else Decimal(0), currency
        ),
        categoryId=draft.category_id,
    )
    obligations_before = [obligations[a.obligation_id] for a in before.allocations]
    obligations_after = []
    for a, o in zip(before.allocations, obligations_before, strict=True):
        delta = subtract(paid[o.id], Decimal(a.cash_paid.amount))
        obligations_after.append(
            o.model_copy(
                update={
                    "cash_paid": _money(_add(Decimal(o.cash_paid.amount), delta), currency),
                    "remaining": _money(subtract(Decimal(o.remaining.amount), delta), currency),
                }
            )
        )
    effects_after = list(effects_before)
    for a in before.allocations:
        delta = subtract(paid[a.obligation_id], Decimal(a.cash_paid.amount))
        if delta:
            effects_after.append(
                _transfer(
                    source,
                    uuid5(source.id, f"correction:{revision + 1}:cash:{a.position}"),
                    obligations[a.obligation_id].receivable_account_id,
                    delta,
                    receiving,
                )
            )
    credit_delta = subtract(
        Decimal(after.retained_credit.amount), Decimal(before.retained_credit.amount)
    )
    if credit_delta:
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
        receivingAccountId=receiving.id,
        transactionDate=source.transaction_date,
        originalConfirmedAt=source.confirmed_at,
        confirmationOrder=source.confirmation_order,
        selectedObligationIds=source.selected_obligation_ids,
        reason=draft.reason,
        excessDisposition=draft.excess_disposition,
        before=before,
        after=after,
        obligationsBefore=obligations_before,
        obligationsAfter=obligations_after,
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
    for a in plan.after.allocations:
        rows[a.position].effective_cash_paid = Decimal(a.cash_paid.amount)
    source.amount = Decimal(command.amount.amount)
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
                    kind="income" if effect.kind == "income" else "internal_transfer",
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
        if effect.kind == "income":
            session.add(
                FinanceCategoryAllocation(
                    transaction_id=effect.transaction_id,
                    ledger_id=ledger_id,
                    category_id=effect.category_id,
                    amount=Decimal(effect.income.amount),
                    currency=source.currency,
                )
            )
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
        else:
            session.add(
                FinanceSettlementCorrectionTransaction(
                    transaction_id=effect.transaction_id,
                    ledger_id=ledger_id,
                    correction_id=record.id,
                )
            )
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
