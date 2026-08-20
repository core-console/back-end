"""Finance Ledger application workflows."""

from collections.abc import Sequence
from datetime import date
from typing import Literal
from uuid import UUID

from psycopg.errors import UniqueViolation
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance.models import FinanceAccount, FinanceCategory, FinanceLedger
from core_console.modules.finance.money import CurrencyCode, Money
from core_console.modules.finance.queries import (
    FinanceAccountBalance,
    get_finance_account_balance,
    get_finance_category,
    get_finance_ledger,
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


class InvalidFinanceCategoryNameError(ValueError):
    """A Category name violates the public Finance contract."""


class FinanceCategoryNameConflictError(Exception):
    """The Ledger already contains a case-insensitively equal Category name."""


class FinanceCategoryNotFoundError(Exception):
    """The addressed Category is missing from the owned addressed Ledger."""


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
    )
    account = balance.account
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
) -> FinanceAccountBalance:
    """Return one in-Ledger Account balance or the safe Account not-found result."""

    balance = await get_finance_account_balance(
        session,
        ledger_id=ledger_id,
        account_id=account_id,
    )
    if balance is None:
        raise FinanceAccountNotFoundError
    return balance


async def _require_finance_category(
    session: AsyncSession,
    *,
    ledger_id: UUID,
    category_id: UUID,
) -> FinanceCategory:
    """Return one in-Ledger Category or the safe Category not-found result."""

    category = await get_finance_category(
        session,
        ledger_id=ledger_id,
        category_id=category_id,
    )
    if category is None:
        raise FinanceCategoryNotFoundError
    return category


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
