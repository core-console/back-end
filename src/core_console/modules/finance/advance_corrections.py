"""Atomic restatement of an Advance with no downstream resolution activity."""

import json
from decimal import Decimal
from hashlib import sha256
from typing import Literal, cast
from uuid import UUID, uuid5

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance.advance_correction_models import FinanceAdvanceCorrection
from core_console.modules.finance.advance_correction_schemas import (
    AdvanceCorrectionCommandV1,
    AdvanceCorrectionDraftV1,
    AdvanceCorrectionPreview,
    AdvanceCorrectionResponse,
    CorrectionAccountEffect,
    CorrectionFinancialEffect,
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
from core_console.modules.finance.settlement_models import FinanceSettlementAllocation


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
    account_ids = {
        source.funding_account_id,
        draft.funding_account_id,
        *(o.receivable_account_id for o in obligations),
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
        source = await _source(session, ledger_id, draft.advance_id)
        obligations = await _obligations(session, source.id)
        if source.funding_account_id not in account_ids or any(
            o.receivable_account_id not in account_ids or o.participant_id not in participant_ids
            for o in obligations
        ):
            raise _problem(
                409,
                "finance_advance_correction_preview_stale",
                "Advance changed. Refresh the complete preview.",
            )
    obligation_ids = [o.id for o in obligations]
    for model in (FinanceSettlementAllocation, FinanceCreditApplicationAllocation):
        if (
            await session.scalar(
                select(model.obligation_id).where(model.obligation_id.in_(obligation_ids)).limit(1)
            )
            is not None
        ):
            raise _problem(
                409,
                "finance_advance_correction_ineligible",
                "This Advance has downstream settlement activity and requires "
                "a later coordinated Correction workflow.",
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
    projected = []
    for id in sorted(old_participants | shares.keys()):
        old = existing.get(id)
        principal = shares.get(id, Decimal(0))
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
                remaining=_money(principal, currency),
                cashPaid=_money(Decimal(0), currency),
                forgiven=_money(Decimal(0), currency),
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
    account_effects = []
    for id in sorted(deltas, key=lambda id: (id != source.funding_account_id, id)):
        balance = balances.get(id)
        value = balance.current_balance if balance else Decimal(0)
        account_effects.append(
            CorrectionAccountEffect(
                accountId=id,
                nature=cast(Literal["asset", "liability"], balance.account.nature)
                if balance
                else "asset",
                balanceBefore=_money(value, currency),
                balanceAfter=_money(subtract(value, subtract(Decimal(0), deltas[id])), currency),
            )
        )
    revision = await session.scalar(
        select(func.coalesce(func.max(FinanceAdvanceCorrection.revision), 0)).where(
            FinanceAdvanceCorrection.advance_id == source.id
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
