"""Narrow read side for filtered deterministic Finance Transaction history."""

import json
from base64 import b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import date
from typing import Literal
from uuid import UUID

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import CTE

from core_console.modules.finance.models import (
    FinanceAccount,
    FinanceAccountMovement,
    FinanceCategory,
    FinanceCategoryAllocation,
    FinanceTransaction,
)
from core_console.modules.finance.queries import FinanceMovementDetail, FinanceTransactionDetail

type TransactionHistoryKind = Literal["income", "expense", "internalTransfer", "balanceAdjustment"]

_CURSOR_VERSION = 1
_ORDERING_CONTEXT = "transactionDate:desc,id:desc"
_DATABASE_KINDS: dict[TransactionHistoryKind, str] = {
    "income": "income",
    "expense": "expense",
    "internalTransfer": "internal_transfer",
    "balanceAdjustment": "balance_adjustment",
}


class InvalidTransactionHistoryCursor(ValueError):
    """A public history cursor is malformed or belongs to other filters."""


@dataclass(frozen=True, slots=True)
class TransactionHistoryQuery:
    """Complete validated filter state for one history request."""

    from_date: date | None
    to_date: date | None
    account_id: UUID | None
    kind: TransactionHistoryKind | None
    category_id: UUID | None
    uncategorized: bool
    cursor: str | None
    page_size: int


@dataclass(frozen=True, slots=True)
class TransactionHistoryPage:
    """One complete read-model page plus its opaque continuation cursor."""

    items: tuple[FinanceTransactionDetail, ...]
    next_cursor: str | None


@dataclass(slots=True)
class _TransactionDetailGroup:
    transaction: FinanceTransaction
    movement_details: list[FinanceMovementDetail]
    movement_ids: set[UUID]
    allocations: list[FinanceCategoryAllocation]
    categories: list[FinanceCategory | None]
    allocation_ids: set[UUID]


def _filter_context(query: TransactionHistoryQuery) -> dict[str, object]:
    return {
        "fromDate": query.from_date.isoformat() if query.from_date is not None else None,
        "toDate": query.to_date.isoformat() if query.to_date is not None else None,
        "accountId": str(query.account_id) if query.account_id is not None else None,
        "kind": query.kind,
        "categoryId": str(query.category_id) if query.category_id is not None else None,
        "uncategorized": query.uncategorized,
    }


def _encode_cursor(
    *,
    ledger_id: UUID,
    transaction_date: date,
    transaction_id: UUID,
    query: TransactionHistoryQuery,
) -> str:
    payload = {
        "v": _CURSOR_VERSION,
        "ordering": _ORDERING_CONTEXT,
        "ledgerId": str(ledger_id),
        "after": [transaction_date.isoformat(), str(transaction_id)],
        "filters": _filter_context(query),
    }
    serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return urlsafe_b64encode(serialized).rstrip(b"=").decode("ascii")


def _decode_cursor(
    cursor: str,
    *,
    ledger_id: UUID,
    query: TransactionHistoryQuery,
) -> tuple[date, UUID]:
    try:
        if not cursor or len(cursor) > 4096:
            raise ValueError
        raw = cursor.encode("ascii")
        raw += b"=" * (-len(raw) % 4)
        payload = json.loads(b64decode(raw, altchars=b"-_", validate=True))
        if not isinstance(payload, dict) or set(payload) != {
            "v",
            "ordering",
            "ledgerId",
            "after",
            "filters",
        }:
            raise ValueError
        if payload["v"] != _CURSOR_VERSION or payload["ordering"] != _ORDERING_CONTEXT:
            raise ValueError
        if payload["ledgerId"] != str(ledger_id):
            raise ValueError
        if payload["filters"] != _filter_context(query):
            raise ValueError
        after = payload["after"]
        if not isinstance(after, list) or len(after) != 2:
            raise ValueError
        raw_date, raw_id = after
        if not isinstance(raw_date, str) or not isinstance(raw_id, str):
            raise ValueError
        transaction_date = date.fromisoformat(raw_date)
        if transaction_date.isoformat() != raw_date:
            raise ValueError
        transaction_id = UUID(raw_id)
        if str(transaction_id) != raw_id:
            raise ValueError
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise InvalidTransactionHistoryCursor from exc
    return transaction_date, transaction_id


async def list_transaction_history(
    session: AsyncSession,
    *,
    ledger_id: UUID,
    query: TransactionHistoryQuery,
) -> TransactionHistoryPage:
    """Read one deterministic keyset page without locks or persistence writes."""

    statement = select(
        FinanceTransaction.id,
        FinanceTransaction.ledger_id,
        FinanceTransaction.transaction_date,
    ).where(FinanceTransaction.ledger_id == ledger_id)
    if query.from_date is not None:
        statement = statement.where(FinanceTransaction.transaction_date >= query.from_date)
    if query.to_date is not None:
        statement = statement.where(FinanceTransaction.transaction_date <= query.to_date)
    if query.kind is not None:
        statement = statement.where(FinanceTransaction.kind == _DATABASE_KINDS[query.kind])
    if query.account_id is not None:
        statement = statement.where(
            exists(
                select(FinanceAccountMovement.id).where(
                    FinanceAccountMovement.transaction_id == FinanceTransaction.id,
                    FinanceAccountMovement.ledger_id == FinanceTransaction.ledger_id,
                    FinanceAccountMovement.account_id == query.account_id,
                )
            )
        )
    if query.category_id is not None:
        statement = statement.where(
            exists(
                select(FinanceCategoryAllocation.id).where(
                    FinanceCategoryAllocation.transaction_id == FinanceTransaction.id,
                    FinanceCategoryAllocation.ledger_id == FinanceTransaction.ledger_id,
                    FinanceCategoryAllocation.category_id == query.category_id,
                )
            )
        )
    if query.uncategorized:
        statement = statement.where(
            FinanceTransaction.kind.in_(("income", "expense")),
            exists(
                select(FinanceCategoryAllocation.id).where(
                    FinanceCategoryAllocation.transaction_id == FinanceTransaction.id,
                    FinanceCategoryAllocation.ledger_id == FinanceTransaction.ledger_id,
                    FinanceCategoryAllocation.category_id.is_(None),
                )
            ),
        )
    if query.cursor is not None:
        cursor_date, cursor_id = _decode_cursor(
            query.cursor,
            ledger_id=ledger_id,
            query=query,
        )
        statement = statement.where(
            or_(
                FinanceTransaction.transaction_date < cursor_date,
                and_(
                    FinanceTransaction.transaction_date == cursor_date,
                    FinanceTransaction.id < cursor_id,
                ),
            )
        )
    statement = statement.order_by(
        FinanceTransaction.transaction_date.desc(), FinanceTransaction.id.desc()
    ).limit(query.page_size + 1)
    page_keys = statement.cte("transaction_history_page")
    all_details = await _get_transaction_details(
        session,
        ledger_id=ledger_id,
        page_keys=page_keys,
    )
    page_details = all_details[: query.page_size]
    if not page_details:
        return TransactionHistoryPage(items=(), next_cursor=None)

    next_cursor = None
    if len(all_details) > query.page_size:
        last = page_details[-1].transaction
        next_cursor = _encode_cursor(
            ledger_id=ledger_id,
            transaction_date=last.transaction_date,
            transaction_id=last.id,
            query=query,
        )
    return TransactionHistoryPage(items=page_details, next_cursor=next_cursor)


async def _get_transaction_details(
    session: AsyncSession,
    *,
    ledger_id: UUID,
    page_keys: CTE,
) -> tuple[FinanceTransactionDetail, ...]:
    """Select page keys and complete aggregates within one statement snapshot."""

    statement = (
        select(
            FinanceTransaction,
            FinanceAccountMovement,
            FinanceAccount,
            FinanceCategoryAllocation,
            FinanceCategory,
        )
        .join(
            page_keys,
            and_(
                FinanceTransaction.id == page_keys.c.id,
                FinanceTransaction.ledger_id == page_keys.c.ledger_id,
            ),
        )
        .join(
            FinanceAccountMovement,
            and_(
                FinanceAccountMovement.transaction_id == FinanceTransaction.id,
                FinanceAccountMovement.ledger_id == FinanceTransaction.ledger_id,
            ),
        )
        .join(
            FinanceAccount,
            and_(
                FinanceAccount.id == FinanceAccountMovement.account_id,
                FinanceAccount.ledger_id == FinanceAccountMovement.ledger_id,
            ),
        )
        .outerjoin(
            FinanceCategoryAllocation,
            and_(
                FinanceCategoryAllocation.transaction_id == FinanceTransaction.id,
                FinanceCategoryAllocation.ledger_id == FinanceTransaction.ledger_id,
            ),
        )
        .outerjoin(
            FinanceCategory,
            and_(
                FinanceCategory.id == FinanceCategoryAllocation.category_id,
                FinanceCategory.ledger_id == FinanceCategoryAllocation.ledger_id,
            ),
        )
        .where(FinanceTransaction.ledger_id == ledger_id)
        .order_by(
            page_keys.c.transaction_date.desc(),
            page_keys.c.id.desc(),
            FinanceAccountMovement.role,
        )
    )
    rows = (await session.execute(statement)).all()
    grouped: dict[UUID, _TransactionDetailGroup] = {}
    for transaction, movement, account, allocation, category in rows:
        group = grouped.setdefault(
            transaction.id,
            _TransactionDetailGroup(
                transaction=transaction,
                movement_details=[],
                movement_ids=set(),
                allocations=[],
                categories=[],
                allocation_ids=set(),
            ),
        )
        if movement.id not in group.movement_ids:
            group.movement_details.append(FinanceMovementDetail(movement=movement, account=account))
            group.movement_ids.add(movement.id)
        if allocation is not None and allocation.id not in group.allocation_ids:
            group.allocations.append(allocation)
            group.categories.append(category)
            group.allocation_ids.add(allocation.id)

    details: list[FinanceTransactionDetail] = []
    for group in grouped.values():
        details.append(
            FinanceTransactionDetail(
                transaction=group.transaction,
                movement_details=tuple(group.movement_details),
                allocations=tuple(group.allocations),
                categories=tuple(group.categories),
            )
        )
    return tuple(details)
