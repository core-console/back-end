"""Advance preview and grouped execution inside the submission transaction."""

import json
from decimal import Decimal
from hashlib import sha256
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance.advance_models import (
    FinanceAdvance,
    FinanceAdvanceTransaction,
    FinanceObligation,
)
from core_console.modules.finance.advance_protection import require_unmanaged_accounts
from core_console.modules.finance.advance_schemas import (
    AdvanceCommandV1,
    AdvanceCommandV2,
    AdvanceDraftV1,
    AdvanceDraftV2,
    AdvancePreview,
    AdvanceResponse,
    ObligationResponse,
    ParticipantReceivablePreview,
    SharedAdvancePreview,
    SharedAdvanceResponse,
)
from core_console.modules.finance.models import (
    FinanceAccount,
    FinanceAccountMovement,
    FinanceCategoryAllocation,
    FinanceLedger,
    FinanceParticipant,
    FinanceTransaction,
)
from core_console.modules.finance.money import CurrencyCode, Money, subtract_money_amounts_exact
from core_console.modules.finance.queries import (
    get_finance_account_balance,
    get_finance_account_for_update,
)
from core_console.modules.finance.schemas import MoneyResponse
from core_console.problems import ApplicationProblem


def _problem(status: int, code: str, detail: str) -> ApplicationProblem:
    from http import HTTPStatus

    return ApplicationProblem(
        status=status,
        title="Unprocessable Entity" if status == 422 else HTTPStatus(status).phrase,
        code=code,
        detail=detail,
    )


async def require_advance_ledger(session: AsyncSession, owner_id: UUID, ledger_id: UUID) -> None:
    if (
        await session.scalar(
            select(FinanceLedger.id).where(
                FinanceLedger.id == ledger_id, FinanceLedger.owner_id == owner_id
            )
        )
        is None
    ):
        raise _problem(404, "finance_ledger_not_found", "Finance Ledger was not found.")


def _money(amount: Decimal, currency: str) -> MoneyResponse:
    value = Money(amount=amount, currency=cast(CurrencyCode, currency))
    return MoneyResponse(amount=value.canonical_amount, currency=value.currency)


def _shares(draft: AdvanceDraftV1 | AdvanceDraftV2) -> list[tuple[UUID, Decimal]]:
    if isinstance(draft, AdvanceDraftV1):
        return [(draft.participant_id, Decimal(draft.participant_share.amount))]
    return sorted(
        [(share.participant_id, Decimal(share.amount.amount)) for share in draft.participant_shares]
    )


async def preview_advance(
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
    draft: AdvanceDraftV1 | AdvanceDraftV2,
    lock: bool = False,
) -> AdvancePreview | SharedAdvancePreview:
    """Recompute consequences; only confirmation locks mutable references."""
    await require_advance_ledger(session, owner_id, ledger_id)
    if lock:
        # Lock the funding Account before the Participant. Ordinary mutations
        # serialize on the same Account; Participant archive locks that row.
        await get_finance_account_for_update(
            session, ledger_id=ledger_id, account_id=draft.funding_account_id
        )
    # Separate statement after the lock is acquired avoids a pre-wait balance snapshot.
    balance = await get_finance_account_balance(
        session, ledger_id=ledger_id, account_id=draft.funding_account_id
    )
    if balance is None:
        raise _problem(404, "finance_account_not_found", "Funding Account was not found.")
    await require_unmanaged_accounts(session, [draft.funding_account_id])
    account = balance.account
    participant_ids = [participant_id for participant_id, _ in _shares(draft)]
    query = (
        select(FinanceParticipant)
        .where(
            FinanceParticipant.id.in_(participant_ids), FinanceParticipant.ledger_id == ledger_id
        )
        .order_by(FinanceParticipant.id)
        .execution_options(populate_existing=True)
    )
    participants = (await session.scalars(query.with_for_update() if lock else query)).all()
    if len(participants) != len(participant_ids):
        raise _problem(404, "finance_participant_not_found", "Participant was not found.")
    if account.status != "active":
        raise _problem(409, "finance_account_archived", "Funding Account is archived.")
    if any(participant.status != "active" for participant in participants):
        raise _problem(
            409, "finance_participant_archived", "New Advances require an active Participant."
        )
    if isinstance(draft, AdvanceDraftV1) and account.nature != "asset":
        raise _problem(422, "validation_error", "T02 requires an Asset funding Account.")
    if draft.amount.currency != account.currency:
        raise _problem(422, "validation_error", "Funding and shares must use the Account currency.")
    if draft.transaction_date < account.tracking_start_date:
        raise _problem(
            422,
            "validation_error",
            "Advance date cannot precede the funding Account Tracking Start Date.",
        )
    intent = (
        AdvanceDraftV1.canonical(draft)
        if isinstance(draft, AdvanceDraftV1)
        else AdvanceDraftV2.canonical(draft)
    )
    state: dict[str, object] = {
        "ledgerId": str(ledger_id),
        "intent": intent,
        "funding": [
            str(account.updated_at),
            account.name,
            account.nature,
            account.currency,
            account.status,
            account.tracking_start_date.isoformat(),
            _money(balance.current_balance, account.currency).amount,
        ],
    }
    if isinstance(draft, AdvanceDraftV1):
        participant = participants[0]
        # Retain the version 1 token grammar for unfinished original actions.
        state["participant"] = [str(participant.updated_at), participant.name, participant.status]
    else:
        state["participants"] = [
            [str(p.id), str(p.updated_at), p.name, p.status] for p in participants
        ]
    token = sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    funding_delta = Decimal(draft.amount.amount)
    if account.nature == "liability":
        funding_delta = subtract_money_amounts_exact(Decimal(0), funding_delta)
    after = subtract_money_amounts_exact(balance.current_balance, funding_delta)
    preview = AdvancePreview(
        previewToken=token,
        fundingBalance=_money(balance.current_balance, account.currency),
        fundingBalanceAfter=_money(after, account.currency),
        ownerExpense=_money(Decimal(draft.owner_share.amount), account.currency),
        receivableIncrease=_money(
            subtract_money_amounts_exact(
                Decimal(draft.amount.amount), Decimal(draft.owner_share.amount)
            ),
            account.currency,
        ),
    )
    if isinstance(draft, AdvanceDraftV1):
        return preview
    return SharedAdvancePreview(
        **preview.model_dump(by_alias=True),
        participantReceivables=[
            ParticipantReceivablePreview(
                participantId=participant_id, principal=_money(principal, account.currency)
            )
            for participant_id, principal in _shares(draft)
        ],
    )


async def execute_create_advance(
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
    command: AdvanceCommandV1 | AdvanceCommandV2,
) -> UUID:
    """Flush every linked effect; the submission owns the sole terminal commit."""
    preview = await preview_advance(
        session, owner_id=owner_id, ledger_id=ledger_id, draft=command, lock=True
    )
    if preview.preview_token != command.preview_token:
        raise _problem(
            409,
            "finance_advance_preview_stale",
            "Advance references or balance changed. Refresh the preview and confirm again.",
        )
    advance_id, transaction_id = uuid4(), uuid4()
    amount = command.amount.to_money()
    funding = await session.get(FinanceAccount, command.funding_account_id)
    assert funding is not None
    negative = funding.nature == "asset"
    for index, (participant_id, principal) in enumerate(_shares(command)):
        obligation_id, account_id = uuid4(), uuid4()
        effect_id = transaction_id if index == 0 else uuid4()
        name = f"Advance receivable {obligation_id}"
        session.add(
            FinanceAccount(
                id=account_id,
                ledger_id=ledger_id,
                name=name,
                name_key=name.casefold(),
                nature="asset",
                currency=amount.currency,
                opening_balance=Decimal(0),
                tracking_start_date=command.transaction_date,
                status="active",
            )
        )
        session.add(
            FinanceTransaction(
                id=effect_id,
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
                    transaction_id=effect_id,
                    ledger_id=ledger_id,
                    account_id=command.funding_account_id,
                    amount=subtract_money_amounts_exact(Decimal(0), principal)
                    if negative
                    else principal,
                    currency=amount.currency,
                    role="source",
                ),
                FinanceAccountMovement(
                    transaction_id=effect_id,
                    ledger_id=ledger_id,
                    account_id=account_id,
                    amount=principal,
                    currency=amount.currency,
                    role="destination",
                ),
            ]
        )
        if index == 0:
            session.add(
                FinanceAdvance(
                    id=advance_id,
                    ledger_id=ledger_id,
                    funding_account_id=command.funding_account_id,
                    transaction_id=transaction_id,
                    transaction_date=command.transaction_date,
                    note=command.note,
                    owner_share=Decimal(command.owner_share.amount),
                    command_version="1" if isinstance(command, AdvanceCommandV1) else "2",
                )
            )
        await session.flush()
        session.add(
            FinanceObligation(
                id=obligation_id,
                ledger_id=ledger_id,
                advance_id=advance_id,
                participant_id=participant_id,
                receivable_account_id=account_id,
                principal=principal,
                currency=amount.currency,
            )
        )
        if isinstance(command, AdvanceCommandV2):
            session.add(
                FinanceAdvanceTransaction(
                    transaction_id=effect_id,
                    ledger_id=ledger_id,
                    advance_id=advance_id,
                )
            )
    owner_share = Decimal(command.owner_share.amount)
    if owner_share > 0:
        expense_id = uuid4()
        session.add(
            FinanceTransaction(
                id=expense_id,
                ledger_id=ledger_id,
                kind="expense",
                transaction_date=command.transaction_date,
                note=command.note,
            )
        )
        await session.flush()
        session.add_all(
            [
                FinanceAccountMovement(
                    transaction_id=expense_id,
                    ledger_id=ledger_id,
                    account_id=command.funding_account_id,
                    amount=subtract_money_amounts_exact(Decimal(0), owner_share)
                    if negative
                    else owner_share,
                    currency=amount.currency,
                    role="primary",
                ),
                FinanceCategoryAllocation(
                    transaction_id=expense_id,
                    ledger_id=ledger_id,
                    category_id=None,
                    amount=owner_share,
                    currency=amount.currency,
                ),
                FinanceAdvanceTransaction(
                    transaction_id=expense_id,
                    ledger_id=ledger_id,
                    advance_id=advance_id,
                ),
            ]
        )
    await session.flush()
    await inspect_advance(session, owner_id=owner_id, ledger_id=ledger_id, advance_id=advance_id)
    return advance_id


def _obligation(advance: FinanceAdvance, obligation: FinanceObligation) -> ObligationResponse:
    principal = _money(obligation.principal, obligation.currency)
    zero = _money(Decimal(0), obligation.currency)
    return ObligationResponse(
        id=obligation.id,
        ledgerId=obligation.ledger_id,
        advanceId=advance.id,
        participantId=obligation.participant_id,
        sourceDate=advance.transaction_date,
        receivableAccountId=obligation.receivable_account_id,
        principal=principal,
        cashPaid=zero,
        forgiven=zero,
        creditApplied=zero,
        remaining=principal,
    )


async def inspect_advance(
    session: AsyncSession, *, owner_id: UUID, ledger_id: UUID, advance_id: UUID
) -> AdvanceResponse | SharedAdvanceResponse:
    await require_advance_ledger(session, owner_id, ledger_id)
    results = (
        await session.execute(
            select(FinanceAdvance, FinanceObligation)
            .join(FinanceObligation, FinanceObligation.advance_id == FinanceAdvance.id)
            .where(FinanceAdvance.id == advance_id, FinanceAdvance.ledger_id == ledger_id)
            .order_by(FinanceObligation.participant_id)
        )
    ).all()
    if not results:
        raise _problem(404, "finance_advance_not_found", "Advance was not found.")
    advance, obligation = results[0]
    total = advance.owner_share
    for _, row in results:
        total = subtract_money_amounts_exact(
            total, subtract_money_amounts_exact(Decimal(0), row.principal)
        )
    source: dict[str, object] = {
        "id": advance.id,
        "ledgerId": ledger_id,
        "fundingAccountId": advance.funding_account_id,
        "transactionDate": advance.transaction_date,
        "note": advance.note,
        "confirmedAt": advance.confirmed_at,
        "amount": _money(total, obligation.currency),
        "ownerShare": _money(advance.owner_share, obligation.currency),
    }
    if advance.command_version == "1":
        return AdvanceResponse.model_validate(
            {
                **source,
                "transactionId": advance.transaction_id,
                "obligation": _obligation(advance, obligation),
            }
        )
    transactions = (
        await session.scalars(
            select(FinanceAdvanceTransaction.transaction_id)
            .where(FinanceAdvanceTransaction.advance_id == advance_id)
            .order_by(FinanceAdvanceTransaction.transaction_id)
        )
    ).all()
    return SharedAdvanceResponse.model_validate(
        {
            **source,
            "transactionIds": list(transactions),
            "obligations": [_obligation(advance, row) for _, row in results],
        }
    )


async def list_participant_obligations(
    session: AsyncSession, *, owner_id: UUID, ledger_id: UUID, participant_id: UUID
) -> list[ObligationResponse]:
    await require_advance_ledger(session, owner_id, ledger_id)
    if (
        await session.scalar(
            select(FinanceParticipant.id).where(
                FinanceParticipant.id == participant_id, FinanceParticipant.ledger_id == ledger_id
            )
        )
        is None
    ):
        raise _problem(404, "finance_participant_not_found", "Participant was not found.")
    rows = (
        await session.execute(
            select(FinanceAdvance, FinanceObligation)
            .join(FinanceObligation, FinanceObligation.advance_id == FinanceAdvance.id)
            .where(
                FinanceObligation.ledger_id == ledger_id,
                FinanceObligation.participant_id == participant_id,
            )
            .order_by(
                FinanceAdvance.transaction_date, FinanceAdvance.confirmed_at, FinanceObligation.id
            )
        )
    ).all()
    return [_obligation(advance, obligation) for advance, obligation in rows]
