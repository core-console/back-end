"""Atomic original-fact restatement and receipt-backed cash releases."""

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
    AdvanceCorrectionCommandV1,
    AdvanceCorrectionDraftV1,
    AdvanceCorrectionPreview,
    AdvanceCorrectionResponse,
    CorrectionAccountEffect,
    CorrectionCashRelease,
    CorrectionFinancialEffect,
    CorrectionForgivenessRestatement,
    CorrectionMovement,
)
from core_console.modules.finance.advance_models import (
    FinanceAdvance,
    FinanceAdvanceTransaction,
    FinanceObligation,
)
from core_console.modules.finance.advance_protection import require_unmanaged_accounts
from core_console.modules.finance.advance_schemas import ObligationResponse, SharedAdvanceResponse
from core_console.modules.finance.advances import (
    _money,
    _problem,
    inspect_advance,
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
)
from core_console.modules.finance.settlement_models import (
    FinanceCreditOrigin,
    FinanceSettlement,
    FinanceSettlementAllocation,
)


def _add(left: Decimal, right: Decimal) -> Decimal:
    return subtract(left, subtract(Decimal(0), right))


async def _settlement_rows(
    session: AsyncSession, obligation_ids: list[UUID]
) -> list[tuple[FinanceSettlementAllocation, FinanceSettlement, FinanceCreditOrigin | None]]:
    rows = await session.execute(
        select(FinanceSettlementAllocation, FinanceSettlement, FinanceCreditOrigin)
        .join(FinanceSettlement, FinanceSettlement.id == FinanceSettlementAllocation.settlement_id)
        .outerjoin(FinanceCreditOrigin, FinanceCreditOrigin.settlement_id == FinanceSettlement.id)
        .where(FinanceSettlementAllocation.obligation_id.in_(obligation_ids))
        .order_by(
            FinanceSettlement.confirmation_order.desc(),
            FinanceSettlementAllocation.position.desc(),
            FinanceSettlement.id.desc(),
        )
        .execution_options(populate_existing=True)
    )
    return [(allocation, settlement, origin) for allocation, settlement, origin in rows]


async def _source(session: AsyncSession, ledger_id: UUID, advance_id: UUID) -> FinanceAdvance:
    source = await session.scalar(
        select(FinanceAdvance)
        .where(FinanceAdvance.id == advance_id, FinanceAdvance.ledger_id == ledger_id)
        .execution_options(populate_existing=True)
    )
    if source is None:
        raise _problem(404, "finance_advance_not_found", "Advance was not found.")
    return source


async def _obligations(session: AsyncSession, advance_id: UUID) -> list[FinanceObligation]:
    return list(
        (
            await session.scalars(
                select(FinanceObligation)
                .where(FinanceObligation.advance_id == advance_id)
                .order_by(FinanceObligation.participant_id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )


async def _effects(
    session: AsyncSession, source: FinanceAdvance
) -> list[CorrectionFinancialEffect]:
    ids = set(
        (
            await session.scalars(
                select(FinanceAdvanceTransaction.transaction_id).where(
                    FinanceAdvanceTransaction.advance_id == source.id
                )
            )
        ).all()
    ) | {source.transaction_id}
    effects = []
    for id in sorted(ids):
        tx = await session.get(FinanceTransaction, id, populate_existing=True)
        assert tx is not None
        movements = (
            await session.scalars(
                select(FinanceAccountMovement)
                .where(FinanceAccountMovement.transaction_id == id)
                .order_by(FinanceAccountMovement.role)
            )
        ).all()
        expense = Decimal(0)
        if tx.kind == "expense":
            expense = (
                await session.scalar(
                    select(func.sum(FinanceCategoryAllocation.amount)).where(
                        FinanceCategoryAllocation.transaction_id == id
                    )
                )
            ) or Decimal(0)
        effects.append(
            CorrectionFinancialEffect.model_validate(
                {
                    "transactionId": id,
                    "kind": "internalTransfer" if tx.kind == "internal_transfer" else tx.kind,
                    "transactionDate": tx.transaction_date.isoformat(),
                    "note": tx.note,
                    "movements": [
                        {
                            "accountId": m.account_id,
                            "amount": _money(m.amount, m.currency),
                            "role": m.role,
                        }
                        for m in movements
                    ],
                    "ownerExpense": _money(expense, movements[0].currency),
                }
            )
        )
    return effects


async def preview_advance_correction(
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
    draft: AdvanceCorrectionDraftV1,
    lock: bool = False,
) -> AdvanceCorrectionPreview:
    await require_advance_ledger(session, owner_id, ledger_id)
    source = await _source(session, ledger_id, draft.advance_id)
    obligations = await _obligations(session, source.id)
    settlement_rows = await _settlement_rows(session, [o.id for o in obligations])
    account_ids = {
        source.funding_account_id,
        draft.funding_account_id,
        *(o.receivable_account_id for o in obligations),
        *(origin.liability_account_id for _, _, origin in settlement_rows if origin is not None),
    }
    participant_ids = {
        *(o.participant_id for o in obligations),
        *(s.participant_id for s in draft.participant_shares),
    }
    if lock:
        # Same order as settlements and manual Finance: Accounts, then Participants.
        # The source lock serializes corrections after all shared financial locks.
        for id in sorted(account_ids):
            await get_finance_account_for_update(session, ledger_id=ledger_id, account_id=id)
        for id in sorted(participant_ids):
            await session.scalar(
                select(FinanceParticipant)
                .where(FinanceParticipant.id == id, FinanceParticipant.ledger_id == ledger_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        await session.scalar(
            select(FinanceAdvance.id).where(FinanceAdvance.id == source.id).with_for_update()
        )
        for settlement_id in sorted({settlement.id for _, settlement, _ in settlement_rows}):
            await session.scalar(
                select(FinanceSettlement.id)
                .where(FinanceSettlement.id == settlement_id)
                .with_for_update()
            )
        source = await _source(session, ledger_id, draft.advance_id)
        obligations = await _obligations(session, source.id)
        current_rows = await _settlement_rows(session, [o.id for o in obligations])
        if (
            source.funding_account_id not in account_ids
            or any(
                o.receivable_account_id not in account_ids
                or o.participant_id not in participant_ids
                for o in obligations
            )
            or any(
                origin is not None and origin.liability_account_id not in account_ids
                for _, _, origin in current_rows
            )
            or not {s.id for _, s, _ in current_rows}.issubset(
                {s.id for _, s, _ in settlement_rows}
            )
        ):
            raise _problem(
                409,
                "finance_advance_correction_preview_stale",
                "Advance changed. Refresh the complete preview.",
            )
        settlement_rows = current_rows
    obligation_ids = [o.id for o in obligations]
    if (
        await session.scalar(
            select(FinanceCreditApplicationAllocation.obligation_id)
            .where(FinanceCreditApplicationAllocation.obligation_id.in_(obligation_ids))
            .limit(1)
        )
        is not None
    ):
        raise _problem(
            409,
            "finance_advance_correction_ineligible",
            "Applied credit requires a later coordinated Correction workflow.",
        )
    for _, settlement, origin in settlement_rows:
        if origin is not None:
            for model in (FinanceCreditApplicationAllocation, FinanceCreditReturnFragment):
                if (
                    await session.scalar(
                        select(model.settlement_id)
                        .where(model.origin_settlement_id == settlement.id)
                        .limit(1)
                    )
                    is not None
                ):
                    raise _problem(
                        409,
                        "finance_advance_correction_ineligible",
                        "Applied or returned receipt credit requires a later "
                        "coordinated Correction.",
                    )
    if settlement_rows and (
        draft.transaction_date != source.transaction_date
        or any(
            s.participant_id not in {o.participant_id for o in obligations}
            for s in draft.participant_shares
        )
    ):
        raise _problem(
            409,
            "finance_advance_correction_ineligible",
            "Changing the date or adding a new debtor after settlement "
            "requires a later Correction workflow.",
        )
    before = await inspect_advance(
        session, owner_id=owner_id, ledger_id=ledger_id, advance_id=source.id
    )
    currency = before.amount.currency
    if draft.amount.currency != currency:
        raise _problem(422, "validation_error", "A Correction cannot change the Advance currency.")
    balances = {}
    for id in sorted(account_ids):
        balance = await get_finance_account_balance(session, ledger_id=ledger_id, account_id=id)
        if balance is None:
            raise _problem(404, "finance_account_not_found", "Correction Account was not found.")
        balances[id] = balance
    await require_unmanaged_accounts(session, [draft.funding_account_id])
    funding = balances[draft.funding_account_id].account
    if funding.currency != currency or draft.transaction_date < funding.tracking_start_date:
        raise _problem(
            422, "validation_error", "Funding currency/date is invalid for the corrected Account."
        )
    if funding.status != "active":
        raise _problem(409, "finance_account_archived", "Corrected funding Account is archived.")
    participants = (
        await session.scalars(
            select(FinanceParticipant)
            .where(
                FinanceParticipant.id.in_(participant_ids),
                FinanceParticipant.ledger_id == ledger_id,
            )
            .order_by(FinanceParticipant.id)
            .execution_options(populate_existing=True)
        )
    ).all()
    if len(participants) != len(participant_ids):
        raise _problem(
            404, "finance_participant_not_found", "Correction Participant was not found."
        )
    old_participants = {o.participant_id for o in obligations}
    if any(
        p.status != "active"
        and p.id in {s.participant_id for s in draft.participant_shares}
        and p.id not in {o.participant_id for o in obligations if o.principal > 0}
        for p in participants
    ):
        raise _problem(
            409, "finance_participant_archived", "New attribution requires an active Participant."
        )
    existing = {o.participant_id: o for o in obligations}
    shares = {s.participant_id: Decimal(s.amount.amount) for s in draft.participant_shares}
    before_obligations = (
        before.obligations if isinstance(before, SharedAdvanceResponse) else [before.obligation]
    )
    previous = {o.id: o for o in before_obligations}
    revision = (
        await session.scalar(
            select(func.coalesce(func.max(FinanceAdvanceCorrection.revision), 0)).where(
                FinanceAdvanceCorrection.advance_id == source.id
            )
        )
    ) or 0
    paid_after = {o.id: Decimal(previous[o.id].cash_paid.amount) for o in obligations}
    forgiven_after = {o.id: Decimal(previous[o.id].forgiven.amount) for o in obligations}
    # Unpaid debt absorbs the reduction until corrected principal falls below resolved debt.
    excess_resolution = {
        o.id: max(
            Decimal(0),
            subtract(
                _add(paid_after[o.id], forgiven_after[o.id]),
                shares.get(o.participant_id, Decimal(0)),
            ),
        )
        for o in obligations
    }
    restatements: list[CorrectionForgivenessRestatement] = []
    for allocation, settlement, _ in settlement_rows:
        if allocation.kind != "forgiveness":
            continue
        id = allocation.obligation_id
        forgiven = (
            allocation.effective_forgiven
            if allocation.effective_forgiven is not None
            else allocation.forgiven
        )
        reduction = min(excess_resolution[id], forgiven)
        if reduction <= 0:
            continue
        assert allocation.transaction_id is not None
        excess_resolution[id] = subtract(excess_resolution[id], reduction)
        forgiven_after[id] = subtract(forgiven_after[id], reduction)
        restatements.append(
            CorrectionForgivenessRestatement(
                settlementId=settlement.id,
                confirmationOrder=settlement.confirmation_order,
                transactionDate=settlement.transaction_date,
                obligationId=id,
                position=allocation.position,
                expenseTransactionId=allocation.transaction_id,
                originalForgiven=_money(allocation.forgiven, currency),
                forgivenBefore=_money(forgiven, currency),
                forgivenAfter=_money(subtract(forgiven, reduction), currency),
            )
        )
    credit_after: dict[UUID, Decimal] = {}
    releases: list[CorrectionCashRelease] = []
    for allocation, settlement, origin in settlement_rows:
        if allocation.kind != "cash":
            continue
        cash = (
            allocation.effective_cash_paid
            if allocation.effective_cash_paid is not None
            else allocation.cash_paid
        )
        id = allocation.obligation_id
        released = min(excess_resolution[id], cash)
        if released <= 0:
            continue
        excess_resolution[id] = subtract(excess_resolution[id], released)
        paid_after[id] = subtract(paid_after[id], released)
        prior_credit = credit_after.get(
            settlement.id,
            (origin.effective_amount if origin.effective_amount is not None else origin.amount)
            if origin
            else Decimal(0),
        )
        new_credit = _add(prior_credit, released)
        credit_after[settlement.id] = new_credit
        credit_account_id = (
            origin.liability_account_id
            if origin
            else uuid5(settlement.id, "correction-credit-account")
        )
        transaction_id = uuid5(
            source.id, f"cash-release:{revision + 1}:{settlement.id}:{allocation.position}"
        )
        receivable = previous[allocation.obligation_id].receivable_account_id
        effect = CorrectionFinancialEffect(
            transactionId=transaction_id,
            kind="internalTransfer",
            transactionDate=settlement.transaction_date,
            note=settlement.note,
            movements=[
                CorrectionMovement(
                    accountId=credit_account_id, amount=_money(released, currency), role="source"
                ),
                CorrectionMovement(
                    accountId=receivable, amount=_money(released, currency), role="destination"
                ),
            ],
            ownerExpense=_money(Decimal(0), currency),
        )
        releases.append(
            CorrectionCashRelease(
                settlementId=settlement.id,
                confirmationOrder=settlement.confirmation_order,
                confirmedAt=settlement.confirmed_at,
                transactionDate=settlement.transaction_date,
                receiptAmount=_money(settlement.amount, currency),
                obligationId=allocation.obligation_id,
                position=allocation.position,
                cashBefore=_money(cash, currency),
                cashAfter=_money(subtract(cash, released), currency),
                released=_money(released, currency),
                creditAccountId=credit_account_id,
                creditBefore=_money(prior_credit, currency),
                creditAfter=_money(new_credit, currency),
                financialEffect=effect,
            )
        )
    if any(excess_resolution.values()):
        raise RuntimeError("Settlement Allocations do not reconcile with the Advance")
    projected = []
    for id in sorted(old_participants | shares.keys()):
        old = existing.get(id)
        principal = shares.get(id, Decimal(0))
        cash_paid = paid_after.get(old.id, Decimal(0)) if old else Decimal(0)
        forgiven = forgiven_after.get(old.id, Decimal(0)) if old else Decimal(0)
        projected.append(
            ObligationResponse(
                id=old.id if old else uuid5(source.id, f"obligation:{id}"),
                ledgerId=ledger_id,
                advanceId=source.id,
                participantId=id,
                sourceDate=draft.transaction_date,
                receivableAccountId=old.receivable_account_id
                if old
                else uuid5(source.id, f"receivable:{id}"),
                principal=_money(principal, currency),
                remaining=_money(subtract(subtract(principal, cash_paid), forgiven), currency),
                cashPaid=_money(cash_paid, currency),
                forgiven=_money(forgiven, currency),
                creditApplied=_money(Decimal(0), currency),
            )
        )
    effects_before = await _effects(session, source)
    effects_after: list[CorrectionFinancialEffect] = []
    for o in projected:
        if Decimal(o.principal.amount) == 0:
            continue
        id = source.transaction_id if not effects_after else uuid5(source.id, f"transfer:{o.id}")
        delta = Decimal(o.principal.amount)
        effects_after.append(
            CorrectionFinancialEffect(
                transactionId=id,
                kind="internalTransfer",
                transactionDate=draft.transaction_date,
                note=draft.note,
                movements=[
                    CorrectionMovement(
                        accountId=draft.funding_account_id,
                        amount=_money(
                            subtract(Decimal(0), delta) if funding.nature == "asset" else delta,
                            currency,
                        ),
                        role="source",
                    ),
                    CorrectionMovement(
                        accountId=o.receivable_account_id, amount=o.principal, role="destination"
                    ),
                ],
                ownerExpense=_money(Decimal(0), currency),
            )
        )
    owner_share = Decimal(draft.owner_share.amount)
    if owner_share > 0:
        id = source.transaction_id if not effects_after else uuid5(source.id, "owner-expense")
        effects_after.append(
            CorrectionFinancialEffect(
                transactionId=id,
                kind="expense",
                transactionDate=draft.transaction_date,
                note=draft.note,
                movements=[
                    CorrectionMovement(
                        accountId=draft.funding_account_id,
                        amount=_money(
                            subtract(Decimal(0), owner_share)
                            if funding.nature == "asset"
                            else owner_share,
                            currency,
                        ),
                        role="primary",
                    )
                ],
                ownerExpense=_money(owner_share, currency),
            )
        )
    after = SharedAdvanceResponse(
        id=source.id,
        ledgerId=ledger_id,
        fundingAccountId=draft.funding_account_id,
        transactionDate=draft.transaction_date,
        note=draft.note,
        confirmedAt=source.confirmed_at,
        amount=_money(Decimal(draft.amount.amount), currency),
        ownerShare=_money(owner_share, currency),
        obligations=projected,
        transactionIds=sorted(e.transaction_id for e in effects_after),
    )
    deltas: dict[UUID, Decimal] = {id: Decimal(0) for id in account_ids}
    for sign, effects in ((-1, effects_before), (1, effects_after)):
        for effect in effects:
            for m in effect.movements:
                value = Decimal(m.amount.amount)
                deltas[m.account_id] = subtract(
                    deltas.get(m.account_id, Decimal(0)),
                    value if sign == -1 else subtract(Decimal(0), value),
                )
    for restatement in restatements:
        id = previous[restatement.obligation_id].receivable_account_id
        deltas[id] = _add(
            deltas[id],
            subtract(
                Decimal(restatement.forgiven_before.amount),
                Decimal(restatement.forgiven_after.amount),
            ),
        )
    for release in releases:
        for movement in release.financial_effect.movements:
            deltas[movement.account_id] = _add(
                deltas.get(movement.account_id, Decimal(0)), Decimal(movement.amount.amount)
            )
    credit_account_ids = {release.credit_account_id for release in releases}
    account_effects = []
    for id in sorted(deltas, key=lambda id: (id != source.funding_account_id, id)):
        balance = balances.get(id)
        value = balance.current_balance if balance else Decimal(0)
        account_effects.append(
            CorrectionAccountEffect(
                accountId=id,
                nature=cast(Literal["asset", "liability"], balance.account.nature)
                if balance
                else "liability"
                if id in credit_account_ids
                else "asset",
                balanceBefore=_money(value, currency),
                balanceAfter=_money(subtract(value, subtract(Decimal(0), deltas[id])), currency),
            )
        )
    data = {
        "advanceId": source.id,
        "reason": draft.reason,
        "fundingChanged": source.funding_account_id != draft.funding_account_id
        or before.amount != after.amount,
        "before": before,
        "after": after,
        "financialEffectsBefore": effects_before,
        "financialEffectsAfter": effects_after,
        "accountEffects": account_effects,
        "cashReleases": releases,
        "forgivenessRestatements": restatements,
    }
    state = {
        "plan": AdvanceCorrectionPreview.model_validate({"previewToken": "", **data}).model_dump(
            mode="json", by_alias=True
        ),
        "intent": AdvanceCorrectionDraftV1.canonical(draft),
        "revision": revision,
        "accounts": [
            [
                str(id),
                str(b.account.updated_at),
                b.account.status,
                b.account.nature,
                b.account.currency,
                b.account.tracking_start_date.isoformat(),
            ]
            for id, b in sorted(balances.items())
        ],
        "participants": [[str(p.id), str(p.updated_at), p.name, p.status] for p in participants],
        "settlementAllocations": [
            [
                str(settlement.id),
                settlement.confirmation_order,
                str(settlement.confirmed_at),
                str(settlement.transaction_date),
                str(settlement.amount),
                str(allocation.obligation_id),
                allocation.position,
                str(allocation.cash_paid),
                str(allocation.effective_cash_paid),
                str(allocation.transaction_id),
                str(allocation.forgiven),
                str(allocation.effective_forgiven),
                str(origin.liability_account_id) if origin else None,
                str(
                    origin.effective_amount
                    if origin.effective_amount is not None
                    else origin.amount
                )
                if origin
                else None,
            ]
            for allocation, settlement, origin in settlement_rows
        ],
    }
    token = sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    return AdvanceCorrectionPreview.model_validate({"previewToken": token, **data})


async def execute_create_advance_correction(
    session: AsyncSession, *, owner_id: UUID, ledger_id: UUID, command: AdvanceCorrectionCommandV1
) -> UUID:
    plan = await preview_advance_correction(
        session, owner_id=owner_id, ledger_id=ledger_id, draft=command, lock=True
    )
    if plan.preview_token != command.preview_token:
        raise _problem(
            409,
            "finance_advance_correction_preview_stale",
            "Correction facts or effects changed. Refresh the complete preview and confirm again.",
        )
    source = await _source(session, ledger_id, command.advance_id)
    old = {o.id: o for o in await _obligations(session, source.id)}
    for o in plan.after.obligations:
        if o.id not in old:
            name = f"Advance receivable {o.id}"
            session.add(
                FinanceAccount(
                    id=o.receivable_account_id,
                    ledger_id=ledger_id,
                    name=name,
                    name_key=name.casefold(),
                    nature="asset",
                    currency=o.principal.currency,
                    opening_balance=Decimal(0),
                    tracking_start_date=o.source_date,
                    status="active",
                )
            )
            await session.flush()
            session.add(
                FinanceObligation(
                    id=o.id,
                    ledger_id=ledger_id,
                    advance_id=source.id,
                    participant_id=o.participant_id,
                    receivable_account_id=o.receivable_account_id,
                    principal=Decimal(o.principal.amount),
                    currency=o.principal.currency,
                )
            )
        else:
            old[o.id].principal = Decimal(o.principal.amount)
            account = await session.get(FinanceAccount, o.receivable_account_id)
            assert account is not None
            account.tracking_start_date = o.source_date
    ids = [e.transaction_id for e in plan.financial_effects_before]
    await session.execute(
        delete(FinanceAdvanceTransaction).where(FinanceAdvanceTransaction.advance_id == source.id)
    )
    await session.execute(
        delete(FinanceCategoryAllocation).where(FinanceCategoryAllocation.transaction_id.in_(ids))
    )
    await session.execute(
        delete(FinanceAccountMovement).where(FinanceAccountMovement.transaction_id.in_(ids))
    )
    await session.execute(
        delete(FinanceTransaction).where(
            FinanceTransaction.id.in_(ids), FinanceTransaction.id != source.transaction_id
        )
    )
    source.funding_account_id = command.funding_account_id
    source.transaction_date = command.transaction_date
    source.note = command.note
    source.owner_share = Decimal(command.owner_share.amount)
    source.command_version = "2"
    for e in plan.financial_effects_after:
        tx = await session.get(FinanceTransaction, e.transaction_id)
        if tx is None:
            tx = FinanceTransaction(id=e.transaction_id, ledger_id=ledger_id)
            session.add(tx)
        tx.kind = "internal_transfer" if e.kind == "internalTransfer" else e.kind
        tx.transaction_date = command.transaction_date
        tx.note = e.note
        await session.flush()
        session.add(
            FinanceAdvanceTransaction(
                transaction_id=e.transaction_id, ledger_id=ledger_id, advance_id=source.id
            )
        )
        for m in e.movements:
            session.add(
                FinanceAccountMovement(
                    transaction_id=e.transaction_id,
                    ledger_id=ledger_id,
                    account_id=m.account_id,
                    amount=Decimal(m.amount.amount),
                    currency=m.amount.currency,
                    role=m.role,
                )
            )
        if e.kind == "expense":
            session.add(
                FinanceCategoryAllocation(
                    transaction_id=e.transaction_id,
                    ledger_id=ledger_id,
                    category_id=None,
                    amount=Decimal(e.owner_expense.amount),
                    currency=e.owner_expense.currency,
                )
            )
    revision = await session.scalar(
        select(func.coalesce(func.max(FinanceAdvanceCorrection.revision), 0)).where(
            FinanceAdvanceCorrection.advance_id == source.id
        )
    )
    record = FinanceAdvanceCorrection(
        ledger_id=ledger_id,
        advance_id=source.id,
        actor_id=owner_id,
        revision=(revision or 0) + 1,
        plan=plan.model_dump(mode="json", by_alias=True),
    )
    session.add(record)
    await session.flush()
    for restatement in plan.forgiveness_restatements:
        allocation = await session.get(
            FinanceSettlementAllocation, (restatement.settlement_id, restatement.position)
        )
        assert (
            allocation is not None
            and allocation.transaction_id == restatement.expense_transaction_id
        )
        amount = Decimal(restatement.forgiven_after.amount)
        allocation.effective_forgiven = amount
        if amount == 0:
            # The correction plan retains the original Expense ID.
            allocation.transaction_id = None
            await session.flush()
            await session.execute(
                delete(FinanceTransaction).where(
                    FinanceTransaction.id == restatement.expense_transaction_id
                )
            )
            continue
        movement = await session.scalar(
            select(FinanceAccountMovement).where(
                FinanceAccountMovement.transaction_id == allocation.transaction_id
            )
        )
        category = await session.scalar(
            select(FinanceCategoryAllocation).where(
                FinanceCategoryAllocation.transaction_id == allocation.transaction_id
            )
        )
        assert movement is not None and category is not None
        movement.amount = subtract(Decimal(0), amount)
        category.amount = amount
    for release in plan.cash_releases:
        allocation = await session.get(
            FinanceSettlementAllocation, (release.settlement_id, release.position)
        )
        assert allocation is not None and allocation.obligation_id == release.obligation_id
        allocation.effective_cash_paid = Decimal(release.cash_after.amount)
        origin = await session.get(FinanceCreditOrigin, release.settlement_id)
        if origin is None:
            name = f"Participant credit {release.settlement_id}"
            session.add(
                FinanceAccount(
                    id=release.credit_account_id,
                    ledger_id=ledger_id,
                    name=name,
                    name_key=name.casefold(),
                    nature="liability",
                    currency=release.released.currency,
                    opening_balance=Decimal(0),
                    tracking_start_date=release.transaction_date,
                    status="active",
                )
            )
        effect = release.financial_effect
        session.add(
            FinanceTransaction(
                id=effect.transaction_id,
                ledger_id=ledger_id,
                kind="internal_transfer",
                transaction_date=effect.transaction_date,
                note=effect.note,
            )
        )
        await session.flush()
        session.add_all(
            [
                FinanceAccountMovement(
                    transaction_id=effect.transaction_id,
                    ledger_id=ledger_id,
                    account_id=movement.account_id,
                    amount=Decimal(movement.amount.amount),
                    currency=movement.amount.currency,
                    role=movement.role,
                )
                for movement in effect.movements
            ]
        )
        if origin is None:
            session.add(
                FinanceCreditOrigin(
                    settlement_id=release.settlement_id,
                    ledger_id=ledger_id,
                    liability_account_id=release.credit_account_id,
                    transaction_id=effect.transaction_id,
                    amount=Decimal(release.credit_after.amount),
                )
            )
        else:
            origin.effective_amount = Decimal(release.credit_after.amount)
        session.add(
            FinanceAdvanceCorrectionTransaction(
                transaction_id=effect.transaction_id,
                ledger_id=ledger_id,
                correction_id=record.id,
                settlement_id=release.settlement_id,
            )
        )
        await session.flush()
    effective = await inspect_advance(
        session, owner_id=owner_id, ledger_id=ledger_id, advance_id=source.id
    )
    if effective != plan.after:
        raise RuntimeError("Correction effects did not match the confirmed plan")
    await inspect_advance_correction(
        session, owner_id=owner_id, ledger_id=ledger_id, correction_id=record.id
    )
    return record.id


def _response(row: FinanceAdvanceCorrection) -> AdvanceCorrectionResponse:
    return AdvanceCorrectionResponse.model_validate(
        {
            **row.plan,
            "id": row.id,
            "ledgerId": row.ledger_id,
            "actorId": row.actor_id,
            "revision": row.revision,
            "confirmedAt": row.confirmed_at,
        }
    )


async def inspect_advance_correction(
    session: AsyncSession, *, owner_id: UUID, ledger_id: UUID, correction_id: UUID
) -> AdvanceCorrectionResponse:
    await require_advance_ledger(session, owner_id, ledger_id)
    row = await session.scalar(
        select(FinanceAdvanceCorrection).where(
            FinanceAdvanceCorrection.id == correction_id,
            FinanceAdvanceCorrection.ledger_id == ledger_id,
        )
    )
    if row is None:
        raise _problem(
            404, "finance_advance_correction_not_found", "Advance Correction was not found."
        )
    return _response(row)


async def list_advance_corrections(
    session: AsyncSession, *, owner_id: UUID, ledger_id: UUID, advance_id: UUID
) -> list[AdvanceCorrectionResponse]:
    await require_advance_ledger(session, owner_id, ledger_id)
    await _source(session, ledger_id, advance_id)
    rows = (
        await session.scalars(
            select(FinanceAdvanceCorrection)
            .where(FinanceAdvanceCorrection.advance_id == advance_id)
            .order_by(FinanceAdvanceCorrection.revision)
        )
    ).all()
    return [_response(row) for row in rows]
