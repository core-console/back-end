"""Finance Ledger application workflows."""

from uuid import UUID

from psycopg.errors import UniqueViolation
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance.models import FinanceLedger
from core_console.modules.finance.queries import get_finance_ledger

_LEDGER_NAME_MAX_LENGTH = 100
_LEDGER_NAME_CONSTRAINT = "uq_finance_ledgers_owner_name_key"


class InvalidFinanceLedgerNameError(ValueError):
    """A Ledger name violates the public Finance contract."""


class FinanceLedgerNameConflictError(Exception):
    """A Local User already owns a case-insensitively equal Ledger name."""


class FinanceLedgerNotFoundError(Exception):
    """The addressed Ledger is missing or not owned by the Current User."""


def normalize_ledger_name(name: str) -> tuple[str, str]:
    """Return the trimmed name and its locale-independent uniqueness key."""

    normalized_name = name.strip()
    if not normalized_name:
        raise InvalidFinanceLedgerNameError("Ledger name must not be blank.")
    if len(normalized_name) > _LEDGER_NAME_MAX_LENGTH:
        raise InvalidFinanceLedgerNameError("Ledger name must not exceed 100 characters.")
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
