"""Finance HTTP routes."""

from collections.abc import Awaitable
from http import HTTPStatus
from typing import Annotated, Never
from uuid import UUID

from fastapi import APIRouter, Depends, Path
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.database.dependencies import get_session
from core_console.modules.finance.currencies import SUPPORTED_CURRENCIES
from core_console.modules.finance.models import FinanceLedger
from core_console.modules.finance.queries import list_finance_ledgers
from core_console.modules.finance.schemas import (
    CreateLedgerRequest,
    CurrencyResponse,
    LedgerResponse,
    UpdateLedgerRequest,
)
from core_console.modules.finance.service import (
    FinanceLedgerNameConflictError,
    FinanceLedgerNotFoundError,
    InvalidFinanceLedgerNameError,
    create_finance_ledger,
    update_finance_ledger,
)
from core_console.modules.users.dependencies import is_database_unavailable, require_active_user
from core_console.modules.users.identity import CurrentUser
from core_console.problems import ApplicationProblem, ProblemDetails

router = APIRouter(prefix="/api/finance", tags=["Finance"])


def _to_ledger_response(ledger: FinanceLedger) -> LedgerResponse:
    """Map persistence state to the closed public Ledger projection."""

    return LedgerResponse(id=ledger.id, name=ledger.name)


async def _run_finance_workflow[Result](workflow: Awaitable[Result]) -> Result:
    """Translate database availability failures at the Finance HTTP boundary."""

    try:
        return await workflow
    except (InterfaceError, OperationalError) as exc:
        if not is_database_unavailable(exc):
            raise
        raise ApplicationProblem(
            status=HTTPStatus.SERVICE_UNAVAILABLE,
            title="Service Unavailable",
            detail="PostgreSQL is not available.",
            code="database_unavailable",
        ) from None


def _raise_invalid_ledger_name(error: InvalidFinanceLedgerNameError) -> Never:
    """Raise the shared validation problem for a Ledger name rule."""

    raise ApplicationProblem(
        status=HTTPStatus.UNPROCESSABLE_ENTITY,
        title="Unprocessable Entity",
        detail=str(error),
        code="validation_error",
    ) from None


def _raise_ledger_name_conflict() -> Never:
    """Raise the stable per-owner Ledger name conflict."""

    raise ApplicationProblem(
        status=HTTPStatus.CONFLICT,
        title="Conflict",
        detail="A Finance Ledger with this name already exists.",
        code="finance_ledger_name_conflict",
    ) from None


def _raise_ledger_not_found() -> Never:
    """Raise the ownership-safe Ledger lookup result."""

    raise ApplicationProblem(
        status=HTTPStatus.NOT_FOUND,
        title="Not Found",
        detail="The requested Finance Ledger does not exist.",
        code="finance_ledger_not_found",
    ) from None


@router.get(
    "/currencies",
    operation_id="listFinanceCurrencies",
    summary="List supported Finance currencies",
    description="Returns the deterministic backend-owned Finance currency catalog.",
    response_model=list[CurrencyResponse],
    responses={
        403: {"model": ProblemDetails, "description": "Access is denied."},
        500: {
            "model": ProblemDetails,
            "description": "An unexpected server error occurred.",
        },
        503: {
            "model": ProblemDetails,
            "description": "PostgreSQL is unavailable or not configured.",
        },
    },
    openapi_extra={"security": []},
)
async def list_currencies(
    _: Annotated[CurrentUser, Depends(require_active_user)],
) -> list[CurrencyResponse]:
    """Return supported currencies in ascending code order."""

    return list(SUPPORTED_CURRENCIES)


@router.get(
    "/ledgers",
    operation_id="listFinanceLedgers",
    summary="List Finance Ledgers",
    description="Returns the Current User's Finance Ledgers in deterministic name order.",
    response_model=list[LedgerResponse],
    responses={
        403: {"model": ProblemDetails, "description": "Access is denied."},
        500: {"model": ProblemDetails, "description": "An unexpected error occurred."},
        503: {"model": ProblemDetails, "description": "PostgreSQL is unavailable."},
    },
    openapi_extra={"security": []},
)
async def get_ledgers(
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[LedgerResponse]:
    """Return only Ledgers personally owned by the Current User."""

    ledgers = await _run_finance_workflow(list_finance_ledgers(session, owner_id=actor.id))
    return [_to_ledger_response(ledger) for ledger in ledgers]


@router.post(
    "/ledgers",
    operation_id="createFinanceLedger",
    summary="Create a Finance Ledger",
    description="Explicitly creates one personally owned Finance Ledger.",
    status_code=HTTPStatus.CREATED,
    response_model=LedgerResponse,
    responses={
        403: {"model": ProblemDetails, "description": "Access is denied."},
        409: {"model": ProblemDetails, "description": "The Ledger name conflicts."},
        422: {"model": ProblemDetails, "description": "The request is invalid."},
        500: {"model": ProblemDetails, "description": "An unexpected error occurred."},
        503: {"model": ProblemDetails, "description": "PostgreSQL is unavailable."},
    },
    openapi_extra={"security": []},
)
async def post_ledger(
    request: CreateLedgerRequest,
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> LedgerResponse:
    """Create one named Ledger for the Current User."""

    try:
        ledger = await _run_finance_workflow(
            create_finance_ledger(session, owner_id=actor.id, name=request.name)
        )
    except InvalidFinanceLedgerNameError as exc:
        _raise_invalid_ledger_name(exc)
    except FinanceLedgerNameConflictError:
        _raise_ledger_name_conflict()
    return _to_ledger_response(ledger)


@router.patch(
    "/ledgers/{ledgerId}",
    operation_id="updateFinanceLedger",
    summary="Rename a Finance Ledger",
    description="Updates only the name of one personally owned Finance Ledger.",
    response_model=LedgerResponse,
    responses={
        403: {"model": ProblemDetails, "description": "Access is denied."},
        404: {"model": ProblemDetails, "description": "The Ledger does not exist."},
        409: {"model": ProblemDetails, "description": "The Ledger name conflicts."},
        422: {"model": ProblemDetails, "description": "The request is invalid."},
        500: {"model": ProblemDetails, "description": "An unexpected error occurred."},
        503: {"model": ProblemDetails, "description": "PostgreSQL is unavailable."},
    },
    openapi_extra={"security": []},
)
async def patch_ledger(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    request: UpdateLedgerRequest,
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> LedgerResponse:
    """Apply an optional name update to one owned Ledger."""

    name = request.name if "name" in request.model_fields_set else None
    try:
        ledger = await _run_finance_workflow(
            update_finance_ledger(
                session,
                owner_id=actor.id,
                ledger_id=ledger_id,
                name=name,
            )
        )
    except FinanceLedgerNotFoundError:
        _raise_ledger_not_found()
    except InvalidFinanceLedgerNameError as exc:
        _raise_invalid_ledger_name(exc)
    except FinanceLedgerNameConflictError:
        _raise_ledger_name_conflict()
    return _to_ledger_response(ledger)
