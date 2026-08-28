"""Finance Ledger application workflows."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal, cast
from uuid import UUID, uuid4

from psycopg.errors import UniqueViolation
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance.models import (
    FinanceAccount,
    FinanceAccountMovement,
    FinanceCategory,
    FinanceCategoryAllocation,
    FinanceLedger,
    FinanceTransaction,
)
from core_console.modules.finance.money import (
    CurrencyCode,
    InvalidMoneyError,
    Money,
    subtract_money_amounts_exact,
)
from core_console.modules.finance.queries import (
    FinanceAccountBalance,
    FinanceTransactionDetail,
    get_earliest_finance_account_transaction_date,
    get_finance_account_balance,
    get_finance_account_balance_at_date,
    get_finance_account_balances_for_update,
    get_finance_account_for_update,
    get_finance_category,
    get_finance_ledger,
    get_finance_transaction_detail,
    get_finance_transaction_for_update,
    has_finance_account_history,
    list_finance_account_balances,
    list_finance_categories,
)

_FINANCE_NAME_MAX_LENGTH = 100
_LEDGER_NAME_CONSTRAINT = "uq_finance_ledgers_owner_name_key"
_CATEGORY_NAME_CONSTRAINT = "uq_finance_categories_ledger_name_key"


class InvalidFinanceLedgerNameError(ValueError):
    """A Ledger name violates the public Finance contract."""


class FinanceLedgerNameConflictError(Exception):
    """A Local User already owns a case-insensitively equal Ledger name."""


class FinanceLedgerNotFoundError(Exception):
    """The addressed Ledger is missing or not owned by the Current User."""


class InvalidFinanceAccountNameError(ValueError):
    """An Account name violates the public Finance contract."""


class InvalidFinanceAccountMoneyError(ValueError):
    """Account Money does not use the Account's currency."""


class FinanceAccountNotFoundError(Exception):
    """The addressed Account is missing from the owned addressed Ledger."""


class FinanceAccountSemanticsLockedError(Exception):
    """Account Nature and Currency cannot change after the Account is locked."""


class FinanceAccountArchivedError(Exception):
    """A new Transaction attempted to use an archived Account."""


class InvalidFinanceAccountTrackingStartDateError(ValueError):
    """An Account Tracking Start Date would exclude associated history."""


class InvalidFinanceCategoryNameError(ValueError):
    """A Category name violates the public Finance contract."""


class FinanceCategoryNameConflictError(Exception):
    """The Ledger already contains a case-insensitively equal Category name."""


class FinanceCategoryNotFoundError(Exception):
    """The addressed Category is missing from the owned addressed Ledger."""


class FinanceCategoryArchivedError(Exception):
    """A new Allocation attempted to use an archived Category."""


class FinanceTransactionNotFoundError(Exception):
    """The addressed Transaction is missing from the owned addressed Ledger."""


class FinanceTransactionKindImmutableError(Exception):
    """The addressed Transaction belongs to a different write workflow."""


class InvalidFinanceTransactionError(ValueError):
    """A Finance Transaction violates the closed Finance contract."""


class FinanceAccountBalanceChangedError(Exception):
    """The expected Balance Adjustment comparison balance became stale."""


class FinanceAccountSemanticsChangedError(Exception):
    """The expected Balance Adjustment Account Nature became stale."""


@dataclass(frozen=True, slots=True)
class BalanceAdjustmentResult[Result]:
    """Outcome-discriminated result from a target-balance command."""

    outcome: Literal["created", "updated", "removed", "noChange"]
    transaction: Result | None


def normalize_ledger_name(name: str) -> tuple[str, str]:
    """Return the trimmed name and its locale-independent uniqueness key."""

    normalized_name = name.strip()
    if not normalized_name:
        raise InvalidFinanceLedgerNameError("Ledger name must not be blank.")
    if len(normalized_name) > _FINANCE_NAME_MAX_LENGTH:
        raise InvalidFinanceLedgerNameError("Ledger name must not exceed 100 characters.")
    return normalized_name, normalized_name.casefold()


def normalize_account_name(name: str) -> tuple[str, str]:
    """Return one trimmed Account name and deterministic ordering key."""

    normalized_name = name.strip()
    if not normalized_name:
        raise InvalidFinanceAccountNameError("Account name must not be blank.")
    if len(normalized_name) > _FINANCE_NAME_MAX_LENGTH:
        raise InvalidFinanceAccountNameError("Account name must not exceed 100 characters.")
    return normalized_name, normalized_name.casefold()


def normalize_category_name(name: str) -> tuple[str, str]:
    """Return one trimmed Category name and locale-independent uniqueness key."""

    normalized_name = name.strip()
    if not normalized_name:
        raise InvalidFinanceCategoryNameError("Category name must not be blank.")
    if len(normalized_name) > _FINANCE_NAME_MAX_LENGTH:
        raise InvalidFinanceCategoryNameError("Category name must not exceed 100 characters.")
    return normalized_name, normalized_name.casefold()


def normalize_transaction_note(note: str | None) -> str | None:
    """Trim optional plain text and preserve the closed null/length semantics."""

    if note is None:
        return None
    normalized_note = note.strip()
    if not normalized_note:
        return None
    if len(normalized_note) > 500:
        raise InvalidFinanceTransactionError("Transaction note must not exceed 500 characters.")
    return normalized_note


def derive_account_movement_amount(
    *,
    kind: Literal["income", "expense"],
    account_nature: Literal["asset", "liability"],
    economic_amount: Money,
) -> Money:
    """Derive one account-relative movement from kind and Account Nature."""

    if not economic_amount.amount.is_finite() or economic_amount.amount <= 0:
        raise InvalidFinanceTransactionError("Economic Amount must be finite and positive.")
    normalized_direction = 1 if kind == "income" else -1
    account_direction = normalized_direction if account_nature == "asset" else -normalized_direction
    return Money(
        amount=economic_amount.amount * account_direction,
        currency=economic_amount.currency,
    )


def derive_internal_transfer_movement_amounts(
    *,
    source_nature: Literal["asset", "liability"],
    destination_nature: Literal["asset", "liability"],
    amount: Money,
) -> tuple[Money, Money]:
    """Derive source and destination changes from normalized economic effects."""

    if not amount.amount.is_finite() or amount.amount <= 0:
        raise InvalidFinanceTransactionError("Transfer amount must be finite and positive.")
    source_direction = -1 if source_nature == "asset" else 1
    destination_direction = 1 if destination_nature == "asset" else -1
    return (
        Money(amount=amount.amount * source_direction, currency=amount.currency),
        Money(amount=amount.amount * destination_direction, currency=amount.currency),
    )


async def create_finance_ledger(
    session: AsyncSession,
    *,
    owner_id: UUID,
    name: str,
) -> FinanceLedger:
    """Create one explicitly named Finance Ledger for a Local User."""

    normalized_name, name_key = normalize_ledger_name(name)
    ledger = FinanceLedger(owner_id=owner_id, name=normalized_name, name_key=name_key)
    session.add(ledger)
    await _commit_ledger_change(session)
    return ledger


async def update_finance_ledger(
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
    name: str | None,
) -> FinanceLedger:
    """Rename an owned Ledger, or return it unchanged when name is omitted."""

    ledger = await get_finance_ledger(session, owner_id=owner_id, ledger_id=ledger_id)
    if ledger is None:
        raise FinanceLedgerNotFoundError
    if name is None:
        return ledger

    ledger.name, ledger.name_key = normalize_ledger_name(name)
    await _commit_ledger_change(session)
    return ledger


async def list_finance_accounts(
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
) -> Sequence[FinanceAccountBalance]:
    """List every active and archived Account in one owned Ledger."""

    await _require_owned_ledger(session, owner_id=owner_id, ledger_id=ledger_id)
    return await list_finance_account_balances(session, ledger_id=ledger_id)


async def create_finance_account(
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
    name: str,
    nature: Literal["asset", "liability"],
    currency: CurrencyCode,
    opening_balance: Money,
    tracking_start_date: date,
) -> FinanceAccountBalance:
    """Create one account-relative position in an owned Ledger."""

    await _require_owned_ledger(session, owner_id=owner_id, ledger_id=ledger_id)
    if opening_balance.currency != currency:
        raise InvalidFinanceAccountMoneyError(
            "Opening Balance currency must match the Account currency."
        )
    normalized_name, name_key = normalize_account_name(name)
    account = FinanceAccount(
        ledger_id=ledger_id,
        name=normalized_name,
        name_key=name_key,
        nature=nature,
        currency=currency,
        opening_balance=opening_balance.amount,
        tracking_start_date=tracking_start_date,
        status="active",
    )
    session.add(account)
    await session.flush()
    balance = await _require_finance_account_balance(
        session,
        ledger_id=ledger_id,
        account_id=account.id,
    )
    await session.commit()
    return balance


async def update_finance_account(
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
    account_id: UUID,
    name: str | None,
    opening_balance: Money | None,
    tracking_start_date: date | None,
) -> FinanceAccountBalance:
    """Apply ordinary mutable Account fields while preserving semantics and status."""

    await _require_owned_ledger(session, owner_id=owner_id, ledger_id=ledger_id)
    balance = await _require_finance_account_balance(
        session,
        ledger_id=ledger_id,
        account_id=account_id,
        for_update=True,
    )
    account = balance.account
    if tracking_start_date is not None:
        earliest_transaction_date = await get_earliest_finance_account_transaction_date(
            session,
            account_id=account.id,
        )
        if (
            earliest_transaction_date is not None
            and tracking_start_date > earliest_transaction_date
        ):
            raise InvalidFinanceAccountTrackingStartDateError(
                "Tracking Start Date cannot be later than associated Transaction history."
            )
    if name is not None:
        account.name, account.name_key = normalize_account_name(name)
    if opening_balance is not None:
        if opening_balance.currency != account.currency:
            raise InvalidFinanceAccountMoneyError(
                "Opening Balance currency must match the Account currency."
            )
        account.opening_balance = opening_balance.amount
    if tracking_start_date is not None:
        account.tracking_start_date = tracking_start_date
    if name is None and opening_balance is None and tracking_start_date is None:
        return balance
    await session.flush()
    updated_balance = await _require_finance_account_balance(
        session,
        ledger_id=ledger_id,
        account_id=account_id,
    )
    await session.commit()
    return updated_balance


async def correct_finance_account_semantics(
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
    account_id: UUID,
    nature: Literal["asset", "liability"] | None,
    currency: CurrencyCode | None,
) -> FinanceAccountBalance:
    """Correct Nature or Currency only while one Account remains semantically unlocked."""

    await _require_owned_ledger(session, owner_id=owner_id, ledger_id=ledger_id)
    balance = await _require_finance_account_balance(
        session,
        ledger_id=ledger_id,
        account_id=account_id,
        for_update=True,
    )
    account = balance.account
    if account.opening_balance != 0 or await has_finance_account_history(
        session,
        account_id=account.id,
    ):
        raise FinanceAccountSemanticsLockedError

    if nature is not None:
        account.nature = nature
    if currency is not None:
        account.currency = currency
    await session.flush()
    updated_balance = await _require_finance_account_balance(
        session,
        ledger_id=ledger_id,
        account_id=account_id,
    )
    await session.commit()
    return updated_balance


async def archive_finance_account(
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
    account_id: UUID,
) -> FinanceAccountBalance:
    """Idempotently archive one owned Account."""

    return await _set_finance_account_status(
        session,
        owner_id=owner_id,
        ledger_id=ledger_id,
        account_id=account_id,
        status="archived",
    )


async def unarchive_finance_account(
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
    account_id: UUID,
) -> FinanceAccountBalance:
    """Idempotently return one owned Account to active status."""

    return await _set_finance_account_status(
        session,
        owner_id=owner_id,
        ledger_id=ledger_id,
        account_id=account_id,
        status="active",
    )


async def list_finance_categories_for_ledger(
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
) -> Sequence[FinanceCategory]:
    """List every active and archived Category in one owned Ledger."""

    await _require_owned_ledger(session, owner_id=owner_id, ledger_id=ledger_id)
    return await list_finance_categories(session, ledger_id=ledger_id)


async def create_finance_category(
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
    name: str,
) -> FinanceCategory:
    """Create one neutral Category in an owned Ledger."""

    await _require_owned_ledger(session, owner_id=owner_id, ledger_id=ledger_id)
    normalized_name, name_key = normalize_category_name(name)
    category = FinanceCategory(
        ledger_id=ledger_id,
        name=normalized_name,
        name_key=name_key,
        status="active",
    )
    session.add(category)
    await _commit_category_change(session)
    return category


async def create_finance_transaction[Result](
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
    kind: Literal["income", "expense"],
    account_id: UUID,
    transaction_date: date,
    economic_amount: Money,
    allocation_amount: Money,
    category_id: UUID | None,
    note: str | None,
    project: Callable[[FinanceTransactionDetail], Result],
) -> Result:
    """Atomically persist and project one complete Income or Expense."""

    await _require_owned_ledger(session, owner_id=owner_id, ledger_id=ledger_id)
    account_balance = await _require_finance_account_balance(
        session,
        ledger_id=ledger_id,
        account_id=account_id,
        for_update=True,
    )
    account = account_balance.account
    if account.status != "active":
        raise FinanceAccountArchivedError
    if transaction_date < account.tracking_start_date:
        raise InvalidFinanceTransactionError(
            "Transaction Date cannot be before the Account Tracking Start Date."
        )
    if economic_amount.currency != account.currency:
        raise InvalidFinanceTransactionError(
            "Economic Amount currency must match the Account currency."
        )
    if (
        not allocation_amount.amount.is_finite()
        or allocation_amount.amount <= 0
        or allocation_amount != economic_amount
    ):
        raise InvalidFinanceTransactionError(
            "The Category Allocation must equal the complete positive Economic Amount."
        )

    category = None
    if category_id is not None:
        category = await _require_finance_category(
            session,
            ledger_id=ledger_id,
            category_id=category_id,
            for_update=True,
        )
        if category.status != "active":
            raise FinanceCategoryArchivedError

    account_nature: Literal["asset", "liability"] = (
        "asset" if account.nature == "asset" else "liability"
    )
    movement_amount = derive_account_movement_amount(
        kind=kind,
        account_nature=account_nature,
        economic_amount=economic_amount,
    )
    transaction_id = uuid4()
    transaction = FinanceTransaction(
        id=transaction_id,
        ledger_id=ledger_id,
        kind=kind,
        transaction_date=transaction_date,
        note=normalize_transaction_note(note),
    )
    movement = FinanceAccountMovement(
        transaction_id=transaction_id,
        ledger_id=ledger_id,
        account_id=account.id,
        amount=movement_amount.amount,
        currency=movement_amount.currency,
        role="primary",
    )
    allocation = FinanceCategoryAllocation(
        transaction_id=transaction_id,
        ledger_id=ledger_id,
        category_id=category.id if category is not None else None,
        amount=allocation_amount.amount,
        currency=allocation_amount.currency,
    )
    return await _persist_and_project_finance_transaction(
        session,
        transaction=transaction,
        children=(movement, allocation),
        project=project,
    )


async def create_internal_transfer_transaction[Result](
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
    source_account_id: UUID,
    destination_account_id: UUID,
    transaction_date: date,
    amount: Money,
    note: str | None,
    project: Callable[[FinanceTransactionDetail], Result],
) -> Result:
    """Atomically persist and project one same-currency Internal Transfer."""

    await _require_owned_ledger(session, owner_id=owner_id, ledger_id=ledger_id)
    if source_account_id == destination_account_id:
        raise InvalidFinanceTransactionError("Source and Destination Accounts must be distinct.")
    balances = await get_finance_account_balances_for_update(
        session,
        ledger_id=ledger_id,
        account_ids=frozenset((source_account_id, destination_account_id)),
    )
    if len(balances) != 2:
        raise FinanceAccountNotFoundError
    by_id = {balance.account.id: balance.account for balance in balances}
    source = by_id[source_account_id]
    destination = by_id[destination_account_id]
    if source.status != "active" or destination.status != "active":
        raise FinanceAccountArchivedError
    if transaction_date < source.tracking_start_date:
        raise InvalidFinanceTransactionError(
            "Transaction Date cannot be before the Source Account Tracking Start Date."
        )
    if transaction_date < destination.tracking_start_date:
        raise InvalidFinanceTransactionError(
            "Transaction Date cannot be before the Destination Account Tracking Start Date."
        )
    if source.currency != destination.currency or amount.currency != source.currency:
        raise InvalidFinanceTransactionError(
            "Transfer Accounts and amount must use the same currency."
        )

    source_nature: Literal["asset", "liability"] = (
        "asset" if source.nature == "asset" else "liability"
    )
    destination_nature: Literal["asset", "liability"] = (
        "asset" if destination.nature == "asset" else "liability"
    )
    source_amount, destination_amount = derive_internal_transfer_movement_amounts(
        source_nature=source_nature,
        destination_nature=destination_nature,
        amount=amount,
    )
    transaction_id = uuid4()
    transaction = FinanceTransaction(
        id=transaction_id,
        ledger_id=ledger_id,
        kind="internal_transfer",
        transaction_date=transaction_date,
        note=normalize_transaction_note(note),
    )
    movements = [
        FinanceAccountMovement(
            transaction_id=transaction_id,
            ledger_id=ledger_id,
            account_id=source.id,
            amount=source_amount.amount,
            currency=source_amount.currency,
            role="source",
        ),
        FinanceAccountMovement(
            transaction_id=transaction_id,
            ledger_id=ledger_id,
            account_id=destination.id,
            amount=destination_amount.amount,
            currency=destination_amount.currency,
            role="destination",
        ),
    ]
    return await _persist_and_project_finance_transaction(
        session,
        transaction=transaction,
        children=movements,
        project=project,
    )


async def get_balance_adjustment_context(
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
    account_id: UUID,
    transaction_date: date,
    replacing_transaction_id: UUID | None,
) -> FinanceAccountBalance:
    """Read one active Account's date-bounded target-command context."""

    await _require_owned_ledger(session, owner_id=owner_id, ledger_id=ledger_id)
    replacing_detail = None
    if replacing_transaction_id is not None:
        replacing_detail = await _require_finance_transaction_detail(
            session,
            ledger_id=ledger_id,
            transaction_id=replacing_transaction_id,
        )
        if replacing_detail.transaction.kind != "balance_adjustment":
            raise InvalidFinanceTransactionError(
                "replacingTransactionId must identify a Balance Adjustment."
            )
    balance = await get_finance_account_balance_at_date(
        session,
        ledger_id=ledger_id,
        account_id=account_id,
        transaction_date=transaction_date,
        excluded_transaction_id=replacing_transaction_id,
    )
    if balance is None:
        raise FinanceAccountNotFoundError
    account = balance.account
    may_retain_archived = replacing_detail is not None and replacing_detail.account.id == account.id
    if account.status != "active" and not may_retain_archived:
        raise FinanceAccountArchivedError
    if transaction_date < account.tracking_start_date:
        raise InvalidFinanceTransactionError(
            "Transaction Date cannot be before the Account Tracking Start Date."
        )
    return balance


async def _validate_and_derive_balance_adjustment_delta(
    session: AsyncSession,
    *,
    ledger_id: UUID,
    account: FinanceAccount,
    transaction_date: date,
    expected_derived_balance: Money,
    expected_account_nature: Literal["asset", "liability"],
    target_balance: Money,
    excluded_transaction_id: UUID | None,
) -> Money:
    """Validate stale-safe Adjustment context and return the exact correction delta."""

    if transaction_date < account.tracking_start_date:
        raise InvalidFinanceTransactionError(
            "Transaction Date cannot be before the Account Tracking Start Date."
        )
    balance = await get_finance_account_balance_at_date(
        session,
        ledger_id=ledger_id,
        account_id=account.id,
        transaction_date=transaction_date,
        excluded_transaction_id=excluded_transaction_id,
    )
    if balance is None:
        raise FinanceAccountNotFoundError
    authoritative = Money(
        amount=balance.current_balance,
        currency=cast(CurrencyCode, account.currency),
    )
    if expected_derived_balance != authoritative:
        raise FinanceAccountBalanceChangedError
    if expected_account_nature != account.nature:
        raise FinanceAccountSemanticsChangedError
    if target_balance.currency != account.currency:
        raise InvalidFinanceTransactionError(
            "Target Balance currency must match the Account currency."
        )
    try:
        return Money(
            amount=subtract_money_amounts_exact(target_balance.amount, authoritative.amount),
            currency=target_balance.currency,
        )
    except InvalidMoneyError as exc:
        raise InvalidFinanceTransactionError(str(exc)) from None


async def create_balance_adjustment[Result](
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
    account_id: UUID,
    transaction_date: date,
    expected_derived_balance: Money,
    expected_account_nature: Literal["asset", "liability"],
    target_balance: Money,
    note: str | None,
    project: Callable[[BalanceAdjustmentResult[FinanceTransactionDetail]], Result],
) -> Result:
    """Atomically create only the non-zero correction to a stale-safe target."""

    await _require_owned_ledger(session, owner_id=owner_id, ledger_id=ledger_id)
    account = await get_finance_account_for_update(
        session,
        ledger_id=ledger_id,
        account_id=account_id,
    )
    if account is None:
        raise FinanceAccountNotFoundError
    if account.status != "active":
        raise FinanceAccountArchivedError
    correction_delta = await _validate_and_derive_balance_adjustment_delta(
        session,
        ledger_id=ledger_id,
        account=account,
        transaction_date=transaction_date,
        expected_derived_balance=expected_derived_balance,
        expected_account_nature=expected_account_nature,
        target_balance=target_balance,
        excluded_transaction_id=None,
    )
    if correction_delta.amount == 0:
        try:
            projected = project(BalanceAdjustmentResult(outcome="noChange", transaction=None))
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        return projected

    transaction_id = uuid4()
    transaction = FinanceTransaction(
        id=transaction_id,
        ledger_id=ledger_id,
        kind="balance_adjustment",
        transaction_date=transaction_date,
        note=normalize_transaction_note(note),
    )
    movement = FinanceAccountMovement(
        transaction_id=transaction_id,
        ledger_id=ledger_id,
        account_id=account.id,
        amount=correction_delta.amount,
        currency=correction_delta.currency,
        role="adjustment",
    )
    return await _persist_and_project_finance_transaction(
        session,
        transaction=transaction,
        children=(movement,),
        project=lambda detail: project(
            BalanceAdjustmentResult(outcome="created", transaction=detail)
        ),
    )


async def replace_balance_adjustment[Result](
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
    transaction_id: UUID,
    account_id: UUID,
    transaction_date: date,
    expected_derived_balance: Money,
    expected_account_nature: Literal["asset", "liability"],
    target_balance: Money,
    note: str | None,
    project: Callable[[BalanceAdjustmentResult[FinanceTransactionDetail]], Result],
) -> Result:
    """Atomically replace or remove one stale-safe Balance Adjustment."""

    await _require_owned_ledger(session, owner_id=owner_id, ledger_id=ledger_id)
    existing = await get_finance_transaction_for_update(
        session,
        ledger_id=ledger_id,
        transaction_id=transaction_id,
    )
    if existing is None:
        raise FinanceTransactionNotFoundError
    if existing.kind != "balance_adjustment":
        raise FinanceTransactionKindImmutableError
    existing_detail = await _require_finance_transaction_detail(
        session,
        ledger_id=ledger_id,
        transaction_id=transaction_id,
    )
    old_account_id = existing_detail.account.id
    locked_accounts = await get_finance_account_balances_for_update(
        session,
        ledger_id=ledger_id,
        account_ids=frozenset((old_account_id, account_id)),
    )
    accounts_by_id = {balance.account.id: balance.account for balance in locked_accounts}
    account = accounts_by_id.get(account_id)
    if account is None:
        raise FinanceAccountNotFoundError
    if account.status != "active" and account_id != old_account_id:
        raise FinanceAccountArchivedError
    correction_delta = await _validate_and_derive_balance_adjustment_delta(
        session,
        ledger_id=ledger_id,
        account=account,
        transaction_date=transaction_date,
        expected_derived_balance=expected_derived_balance,
        expected_account_nature=expected_account_nature,
        target_balance=target_balance,
        excluded_transaction_id=transaction_id,
    )

    try:
        await session.delete(existing)
        await session.flush()
        if correction_delta.amount == 0:
            projected = project(BalanceAdjustmentResult(outcome="removed", transaction=None))
            await session.commit()
            return projected

        replacement = FinanceTransaction(
            id=transaction_id,
            ledger_id=ledger_id,
            kind="balance_adjustment",
            transaction_date=transaction_date,
            note=normalize_transaction_note(note),
        )
        movement = FinanceAccountMovement(
            transaction_id=transaction_id,
            ledger_id=ledger_id,
            account_id=account.id,
            amount=correction_delta.amount,
            currency=correction_delta.currency,
            role="adjustment",
        )
        return await _persist_and_project_finance_transaction(
            session,
            transaction=replacement,
            children=(movement,),
            project=lambda detail: project(
                BalanceAdjustmentResult(outcome="updated", transaction=detail)
            ),
        )
    except Exception:
        await session.rollback()
        raise


async def get_finance_transaction(
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
    transaction_id: UUID,
) -> FinanceTransactionDetail:
    """Read one complete Income or Expense through ownership-safe scope."""

    await _require_owned_ledger(session, owner_id=owner_id, ledger_id=ledger_id)
    return await _require_finance_transaction_detail(
        session,
        ledger_id=ledger_id,
        transaction_id=transaction_id,
    )


async def _persist_and_project_finance_transaction[Result](
    session: AsyncSession,
    *,
    transaction: FinanceTransaction,
    children: Sequence[FinanceAccountMovement | FinanceCategoryAllocation],
    project: Callable[[FinanceTransactionDetail], Result],
) -> Result:
    """Commit only after the complete aggregate can produce its public result."""

    session.add(transaction)
    try:
        await session.flush()
        session.add_all(children)
        await session.flush()
        detail = await _require_finance_transaction_detail(
            session,
            ledger_id=transaction.ledger_id,
            transaction_id=transaction.id,
        )
        projected = project(detail)
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return projected


async def update_finance_category(
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
    category_id: UUID,
    name: str | None,
) -> FinanceCategory:
    """Rename one Category while preserving its lifecycle status."""

    await _require_owned_ledger(session, owner_id=owner_id, ledger_id=ledger_id)
    category = await _require_finance_category(
        session,
        ledger_id=ledger_id,
        category_id=category_id,
        for_update=True,
    )
    if name is None:
        return category
    category.name, category.name_key = normalize_category_name(name)
    await _commit_category_change(session)
    return category


async def archive_finance_category(
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
    category_id: UUID,
) -> FinanceCategory:
    """Idempotently archive one owned Category."""

    return await _set_finance_category_status(
        session,
        owner_id=owner_id,
        ledger_id=ledger_id,
        category_id=category_id,
        status="archived",
    )


async def unarchive_finance_category(
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
    category_id: UUID,
) -> FinanceCategory:
    """Idempotently return one owned Category to active status."""

    return await _set_finance_category_status(
        session,
        owner_id=owner_id,
        ledger_id=ledger_id,
        category_id=category_id,
        status="active",
    )


async def _set_finance_category_status(
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
    category_id: UUID,
    status: Literal["active", "archived"],
) -> FinanceCategory:
    """Set Category lifecycle status after ownership-safe lookup."""

    await _require_owned_ledger(session, owner_id=owner_id, ledger_id=ledger_id)
    category = await _require_finance_category(
        session,
        ledger_id=ledger_id,
        category_id=category_id,
        for_update=True,
    )
    if category.status == status:
        return category
    if status == "active":
        category.name, category.name_key = normalize_category_name(category.name)
    category.status = status
    await _commit_category_change(session)
    return category


async def _set_finance_account_status(
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
    account_id: UUID,
    status: Literal["active", "archived"],
) -> FinanceAccountBalance:
    """Set Account lifecycle status after ownership-safe lookup."""

    await _require_owned_ledger(session, owner_id=owner_id, ledger_id=ledger_id)
    balance = await _require_finance_account_balance(
        session,
        ledger_id=ledger_id,
        account_id=account_id,
        for_update=True,
    )
    if balance.account.status == status:
        return balance
    balance.account.status = status
    await session.flush()
    updated_balance = await _require_finance_account_balance(
        session,
        ledger_id=ledger_id,
        account_id=account_id,
    )
    await session.commit()
    return updated_balance


async def _require_owned_ledger(
    session: AsyncSession,
    *,
    owner_id: UUID,
    ledger_id: UUID,
) -> FinanceLedger:
    """Return an owned Ledger or preserve the non-leaking lookup contract."""

    ledger = await get_finance_ledger(session, owner_id=owner_id, ledger_id=ledger_id)
    if ledger is None:
        raise FinanceLedgerNotFoundError
    return ledger


async def _require_finance_account_balance(
    session: AsyncSession,
    *,
    ledger_id: UUID,
    account_id: UUID,
    for_update: bool = False,
) -> FinanceAccountBalance:
    """Return one in-Ledger Account balance or the safe Account not-found result."""

    balance = await get_finance_account_balance(
        session,
        ledger_id=ledger_id,
        account_id=account_id,
        for_update=for_update,
    )
    if balance is None:
        raise FinanceAccountNotFoundError
    return balance


async def _require_finance_category(
    session: AsyncSession,
    *,
    ledger_id: UUID,
    category_id: UUID,
    for_update: bool = False,
) -> FinanceCategory:
    """Return one in-Ledger Category or the safe Category not-found result."""

    category = await get_finance_category(
        session,
        ledger_id=ledger_id,
        category_id=category_id,
        for_update=for_update,
    )
    if category is None:
        raise FinanceCategoryNotFoundError
    return category


async def _require_finance_transaction_detail(
    session: AsyncSession,
    *,
    ledger_id: UUID,
    transaction_id: UUID,
) -> FinanceTransactionDetail:
    """Return a complete in-Ledger Transaction or the safe not-found result."""

    detail = await get_finance_transaction_detail(
        session,
        ledger_id=ledger_id,
        transaction_id=transaction_id,
    )
    if detail is None:
        raise FinanceTransactionNotFoundError
    return detail


async def _commit_ledger_change(session: AsyncSession) -> None:
    """Commit one Ledger mutation and translate its owned-name invariant."""

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if (
            isinstance(exc.orig, UniqueViolation)
            and exc.orig.diag.constraint_name == _LEDGER_NAME_CONSTRAINT
        ):
            raise FinanceLedgerNameConflictError from None
        raise


async def _commit_category_change(session: AsyncSession) -> None:
    """Commit one Category mutation and translate its all-status name invariant."""

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if (
            isinstance(exc.orig, UniqueViolation)
            and exc.orig.diag.constraint_name == _CATEGORY_NAME_CONSTRAINT
        ):
            raise FinanceCategoryNameConflictError from None
        raise
