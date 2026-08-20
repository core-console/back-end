"""Finance HTTP routes."""

from collections.abc import Awaitable
from decimal import Decimal
from http import HTTPStatus
from typing import Annotated, Literal, Never, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Path
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.database.dependencies import get_session
from core_console.modules.finance.currencies import SUPPORTED_CURRENCIES
from core_console.modules.finance.models import FinanceCategory, FinanceLedger
from core_console.modules.finance.money import CurrencyCode, Money
from core_console.modules.finance.queries import FinanceAccountBalance, list_finance_ledgers
from core_console.modules.finance.schemas import (
    AccountResponse,
    CategoryResponse,
    CreateAccountRequest,
    CreateCategoryRequest,
    CreateLedgerRequest,
    CurrencyResponse,
    LedgerResponse,
    MoneyResponse,
    UpdateAccountRequest,
    UpdateCategoryRequest,
    UpdateLedgerRequest,
)
from core_console.modules.finance.service import (
    FinanceAccountNotFoundError,
    FinanceCategoryNameConflictError,
    FinanceCategoryNotFoundError,
    FinanceLedgerNameConflictError,
    FinanceLedgerNotFoundError,
    InvalidFinanceAccountMoneyError,
    InvalidFinanceAccountNameError,
    InvalidFinanceCategoryNameError,
    InvalidFinanceLedgerNameError,
    archive_finance_account,
    archive_finance_category,
    create_finance_account,
    create_finance_category,
    create_finance_ledger,
    list_finance_accounts,
    list_finance_categories_for_ledger,
    unarchive_finance_account,
    unarchive_finance_category,
    update_finance_account,
    update_finance_category,
    update_finance_ledger,
)
from core_console.modules.users.dependencies import is_database_unavailable, require_active_user
from core_console.modules.users.identity import CurrentUser
from core_console.problems import ApplicationProblem, ProblemDetails

router = APIRouter(prefix="/api/finance", tags=["Finance"])


def _to_ledger_response(ledger: FinanceLedger) -> LedgerResponse:
    """Map persistence state to the closed public Ledger projection."""

    return LedgerResponse(id=ledger.id, name=ledger.name)


def _to_money_response(amount: Decimal, currency: str) -> MoneyResponse:
    """Map exact persistence state to canonical public Money."""

    money = Money(amount=amount, currency=cast(CurrencyCode, currency))
    return MoneyResponse(amount=money.canonical_amount, currency=money.currency)


def _to_account_response(balance: FinanceAccountBalance) -> AccountResponse:
    """Map persistence and derivation state to the closed Account projection."""

    account = balance.account
    return AccountResponse(
        id=account.id,
        name=account.name,
        nature=cast(Literal["asset", "liability"], account.nature),
        currency=cast(CurrencyCode, account.currency),
        openingBalance=_to_money_response(account.opening_balance, account.currency),
        trackingStartDate=account.tracking_start_date,
        currentBalance=_to_money_response(balance.current_balance, account.currency),
        status=cast(Literal["active", "archived"], account.status),
    )


def _to_category_response(category: FinanceCategory) -> CategoryResponse:
    """Map persistence state to the closed neutral Category projection."""

    return CategoryResponse(
        id=category.id,
        name=category.name,
        status=cast(Literal["active", "archived"], category.status),
    )


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


def _raise_invalid_account(error: ValueError) -> Never:
    """Raise the shared validation problem for an Account rule."""

    raise ApplicationProblem(
        status=HTTPStatus.UNPROCESSABLE_ENTITY,
        title="Unprocessable Entity",
        detail=str(error),
        code="validation_error",
    ) from None


def _raise_account_not_found() -> Never:
    """Raise the non-leaking nested Account lookup result."""

    raise ApplicationProblem(
        status=HTTPStatus.NOT_FOUND,
        title="Not Found",
        detail="The requested Finance Account does not exist.",
        code="finance_account_not_found",
    ) from None


def _raise_invalid_category(error: InvalidFinanceCategoryNameError) -> Never:
    """Raise the shared validation problem for a Category name rule."""

    raise ApplicationProblem(
        status=HTTPStatus.UNPROCESSABLE_ENTITY,
        title="Unprocessable Entity",
        detail=str(error),
        code="validation_error",
    ) from None


def _raise_category_name_conflict() -> Never:
    """Raise the stable all-status per-Ledger Category name conflict."""

    raise ApplicationProblem(
        status=HTTPStatus.CONFLICT,
        title="Conflict",
        detail="A Finance Category with this name already exists.",
        code="finance_category_name_conflict",
    ) from None


def _raise_category_not_found() -> Never:
    """Raise the non-leaking nested Category lookup result."""

    raise ApplicationProblem(
        status=HTTPStatus.NOT_FOUND,
        title="Not Found",
        detail="The requested Finance Category does not exist.",
        code="finance_category_not_found",
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


@router.get(
    "/ledgers/{ledgerId}/accounts",
    operation_id="listFinanceAccounts",
    summary="List Finance Accounts",
    description="Returns active and archived Accounts with derived Current Balances.",
    response_model=list[AccountResponse],
    responses={
        403: {"model": ProblemDetails, "description": "Access is denied."},
        404: {"model": ProblemDetails, "description": "The Ledger does not exist."},
        422: {"model": ProblemDetails, "description": "The path is invalid."},
        500: {"model": ProblemDetails, "description": "An unexpected error occurred."},
        503: {"model": ProblemDetails, "description": "PostgreSQL is unavailable."},
    },
    openapi_extra={"security": []},
)
async def get_accounts(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[AccountResponse]:
    """List every Account in one owned Ledger."""

    try:
        balances = await _run_finance_workflow(
            list_finance_accounts(
                session,
                owner_id=actor.id,
                ledger_id=ledger_id,
            )
        )
    except FinanceLedgerNotFoundError:
        _raise_ledger_not_found()
    return [_to_account_response(balance) for balance in balances]


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


@router.post(
    "/ledgers/{ledgerId}/accounts",
    operation_id="createFinanceAccount",
    summary="Create a Finance Account",
    description="Creates one account-relative position inside an owned Finance Ledger.",
    status_code=HTTPStatus.CREATED,
    response_model=AccountResponse,
    responses={
        403: {"model": ProblemDetails, "description": "Access is denied."},
        404: {"model": ProblemDetails, "description": "The Ledger does not exist."},
        422: {"model": ProblemDetails, "description": "The request is invalid."},
        500: {"model": ProblemDetails, "description": "An unexpected error occurred."},
        503: {"model": ProblemDetails, "description": "PostgreSQL is unavailable."},
    },
    openapi_extra={"security": []},
)
async def post_account(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    request: CreateAccountRequest,
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AccountResponse:
    """Create one Account in an owned Ledger."""

    try:
        balance = await _run_finance_workflow(
            create_finance_account(
                session,
                owner_id=actor.id,
                ledger_id=ledger_id,
                name=request.name,
                nature=request.nature,
                currency=request.currency,
                opening_balance=request.opening_balance.to_money(),
                tracking_start_date=request.tracking_start_date,
            )
        )
    except FinanceLedgerNotFoundError:
        _raise_ledger_not_found()
    except (InvalidFinanceAccountNameError, InvalidFinanceAccountMoneyError) as exc:
        _raise_invalid_account(exc)
    return _to_account_response(balance)


@router.patch(
    "/ledgers/{ledgerId}/accounts/{accountId}",
    operation_id="updateFinanceAccount",
    summary="Update a Finance Account",
    description="Updates only ordinary mutable properties of one owned Account.",
    response_model=AccountResponse,
    responses={
        403: {"model": ProblemDetails, "description": "Access is denied."},
        404: {"model": ProblemDetails, "description": "The resource does not exist."},
        422: {"model": ProblemDetails, "description": "The request is invalid."},
        500: {"model": ProblemDetails, "description": "An unexpected error occurred."},
        503: {"model": ProblemDetails, "description": "PostgreSQL is unavailable."},
    },
    openapi_extra={"security": []},
)
async def patch_account(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    account_id: Annotated[UUID, Path(alias="accountId")],
    request: UpdateAccountRequest,
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AccountResponse:
    """Apply omitted-field ordinary editing to one owned Account."""

    fields = request.model_fields_set
    opening_balance = request.opening_balance.to_money() if "opening_balance" in fields else None
    try:
        balance = await _run_finance_workflow(
            update_finance_account(
                session,
                owner_id=actor.id,
                ledger_id=ledger_id,
                account_id=account_id,
                name=request.name if "name" in fields else None,
                opening_balance=opening_balance,
                tracking_start_date=(
                    request.tracking_start_date if "tracking_start_date" in fields else None
                ),
            )
        )
    except FinanceLedgerNotFoundError:
        _raise_ledger_not_found()
    except FinanceAccountNotFoundError:
        _raise_account_not_found()
    except (InvalidFinanceAccountNameError, InvalidFinanceAccountMoneyError) as exc:
        _raise_invalid_account(exc)
    return _to_account_response(balance)


async def _change_account_status(
    *,
    ledger_id: UUID,
    account_id: UUID,
    actor: CurrentUser,
    session: AsyncSession,
    status: Literal["active", "archived"],
) -> AccountResponse:
    """Run one Account lifecycle command with shared error translation."""

    workflow = archive_finance_account if status == "archived" else unarchive_finance_account
    try:
        balance = await _run_finance_workflow(
            workflow(
                session,
                owner_id=actor.id,
                ledger_id=ledger_id,
                account_id=account_id,
            )
        )
    except FinanceLedgerNotFoundError:
        _raise_ledger_not_found()
    except FinanceAccountNotFoundError:
        _raise_account_not_found()
    return _to_account_response(balance)


@router.post(
    "/ledgers/{ledgerId}/accounts/{accountId}/archive",
    operation_id="archiveFinanceAccount",
    summary="Archive a Finance Account",
    description="Idempotently archives one owned Account without erasing its balance.",
    response_model=AccountResponse,
    responses={
        403: {"model": ProblemDetails, "description": "Access is denied."},
        404: {"model": ProblemDetails, "description": "The resource does not exist."},
        422: {"model": ProblemDetails, "description": "The path is invalid."},
        500: {"model": ProblemDetails, "description": "An unexpected error occurred."},
        503: {"model": ProblemDetails, "description": "PostgreSQL is unavailable."},
    },
    openapi_extra={"security": []},
)
async def post_account_archive(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    account_id: Annotated[UUID, Path(alias="accountId")],
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AccountResponse:
    """Archive one Account idempotently."""

    return await _change_account_status(
        ledger_id=ledger_id,
        account_id=account_id,
        actor=actor,
        session=session,
        status="archived",
    )


@router.post(
    "/ledgers/{ledgerId}/accounts/{accountId}/unarchive",
    operation_id="unarchiveFinanceAccount",
    summary="Unarchive a Finance Account",
    description="Idempotently restores one owned Account to active status.",
    response_model=AccountResponse,
    responses={
        403: {"model": ProblemDetails, "description": "Access is denied."},
        404: {"model": ProblemDetails, "description": "The resource does not exist."},
        422: {"model": ProblemDetails, "description": "The path is invalid."},
        500: {"model": ProblemDetails, "description": "An unexpected error occurred."},
        503: {"model": ProblemDetails, "description": "PostgreSQL is unavailable."},
    },
    openapi_extra={"security": []},
)
async def post_account_unarchive(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    account_id: Annotated[UUID, Path(alias="accountId")],
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AccountResponse:
    """Unarchive one Account idempotently."""

    return await _change_account_status(
        ledger_id=ledger_id,
        account_id=account_id,
        actor=actor,
        session=session,
        status="active",
    )


@router.get(
    "/ledgers/{ledgerId}/categories",
    operation_id="listFinanceCategories",
    summary="List Finance Categories",
    description="Returns active and archived neutral Categories in deterministic order.",
    response_model=list[CategoryResponse],
    responses={
        403: {"model": ProblemDetails, "description": "Access is denied."},
        404: {"model": ProblemDetails, "description": "The Ledger does not exist."},
        422: {"model": ProblemDetails, "description": "The path is invalid."},
        500: {"model": ProblemDetails, "description": "An unexpected error occurred."},
        503: {"model": ProblemDetails, "description": "PostgreSQL is unavailable."},
    },
    openapi_extra={"security": []},
)
async def get_categories(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[CategoryResponse]:
    """List every Category in one owned Ledger."""

    try:
        categories = await _run_finance_workflow(
            list_finance_categories_for_ledger(
                session,
                owner_id=actor.id,
                ledger_id=ledger_id,
            )
        )
    except FinanceLedgerNotFoundError:
        _raise_ledger_not_found()
    return [_to_category_response(category) for category in categories]


@router.post(
    "/ledgers/{ledgerId}/categories",
    operation_id="createFinanceCategory",
    summary="Create a Finance Category",
    description="Creates one neutral Category inside an owned Finance Ledger.",
    status_code=HTTPStatus.CREATED,
    response_model=CategoryResponse,
    responses={
        403: {"model": ProblemDetails, "description": "Access is denied."},
        404: {"model": ProblemDetails, "description": "The Ledger does not exist."},
        409: {"model": ProblemDetails, "description": "The Category name conflicts."},
        422: {"model": ProblemDetails, "description": "The request is invalid."},
        500: {"model": ProblemDetails, "description": "An unexpected error occurred."},
        503: {"model": ProblemDetails, "description": "PostgreSQL is unavailable."},
    },
    openapi_extra={"security": []},
)
async def post_category(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    request: CreateCategoryRequest,
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CategoryResponse:
    """Create one neutral Category in an owned Ledger."""

    try:
        category = await _run_finance_workflow(
            create_finance_category(
                session,
                owner_id=actor.id,
                ledger_id=ledger_id,
                name=request.name,
            )
        )
    except FinanceLedgerNotFoundError:
        _raise_ledger_not_found()
    except InvalidFinanceCategoryNameError as exc:
        _raise_invalid_category(exc)
    except FinanceCategoryNameConflictError:
        _raise_category_name_conflict()
    return _to_category_response(category)


@router.patch(
    "/ledgers/{ledgerId}/categories/{categoryId}",
    operation_id="updateFinanceCategory",
    summary="Update a Finance Category",
    description="Renames one neutral Category without changing its status.",
    response_model=CategoryResponse,
    responses={
        403: {"model": ProblemDetails, "description": "Access is denied."},
        404: {"model": ProblemDetails, "description": "The resource does not exist."},
        409: {"model": ProblemDetails, "description": "The Category name conflicts."},
        422: {"model": ProblemDetails, "description": "The request is invalid."},
        500: {"model": ProblemDetails, "description": "An unexpected error occurred."},
        503: {"model": ProblemDetails, "description": "PostgreSQL is unavailable."},
    },
    openapi_extra={"security": []},
)
async def patch_category(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    category_id: Annotated[UUID, Path(alias="categoryId")],
    request: UpdateCategoryRequest,
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CategoryResponse:
    """Apply an optional name update to one owned Category."""

    name = request.name if "name" in request.model_fields_set else None
    try:
        category = await _run_finance_workflow(
            update_finance_category(
                session,
                owner_id=actor.id,
                ledger_id=ledger_id,
                category_id=category_id,
                name=name,
            )
        )
    except FinanceLedgerNotFoundError:
        _raise_ledger_not_found()
    except FinanceCategoryNotFoundError:
        _raise_category_not_found()
    except InvalidFinanceCategoryNameError as exc:
        _raise_invalid_category(exc)
    except FinanceCategoryNameConflictError:
        _raise_category_name_conflict()
    return _to_category_response(category)


async def _change_category_status(
    *,
    ledger_id: UUID,
    category_id: UUID,
    actor: CurrentUser,
    session: AsyncSession,
    status: Literal["active", "archived"],
) -> CategoryResponse:
    """Run one Category lifecycle command with shared error translation."""

    workflow = archive_finance_category if status == "archived" else unarchive_finance_category
    try:
        category = await _run_finance_workflow(
            workflow(
                session,
                owner_id=actor.id,
                ledger_id=ledger_id,
                category_id=category_id,
            )
        )
    except FinanceLedgerNotFoundError:
        _raise_ledger_not_found()
    except FinanceCategoryNotFoundError:
        _raise_category_not_found()
    except FinanceCategoryNameConflictError:
        _raise_category_name_conflict()
    return _to_category_response(category)


@router.post(
    "/ledgers/{ledgerId}/categories/{categoryId}/archive",
    operation_id="archiveFinanceCategory",
    summary="Archive a Finance Category",
    description="Idempotently archives one neutral Category without deleting it.",
    response_model=CategoryResponse,
    responses={
        403: {"model": ProblemDetails, "description": "Access is denied."},
        404: {"model": ProblemDetails, "description": "The resource does not exist."},
        422: {"model": ProblemDetails, "description": "The path is invalid."},
        500: {"model": ProblemDetails, "description": "An unexpected error occurred."},
        503: {"model": ProblemDetails, "description": "PostgreSQL is unavailable."},
    },
    openapi_extra={"security": []},
)
async def post_category_archive(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    category_id: Annotated[UUID, Path(alias="categoryId")],
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CategoryResponse:
    """Archive one Category idempotently."""

    return await _change_category_status(
        ledger_id=ledger_id,
        category_id=category_id,
        actor=actor,
        session=session,
        status="archived",
    )


@router.post(
    "/ledgers/{ledgerId}/categories/{categoryId}/unarchive",
    operation_id="unarchiveFinanceCategory",
    summary="Unarchive a Finance Category",
    description="Idempotently restores one Category after rechecking name uniqueness.",
    response_model=CategoryResponse,
    responses={
        403: {"model": ProblemDetails, "description": "Access is denied."},
        404: {"model": ProblemDetails, "description": "The resource does not exist."},
        409: {"model": ProblemDetails, "description": "The Category name conflicts."},
        422: {"model": ProblemDetails, "description": "The path is invalid."},
        500: {"model": ProblemDetails, "description": "An unexpected error occurred."},
        503: {"model": ProblemDetails, "description": "PostgreSQL is unavailable."},
    },
    openapi_extra={"security": []},
)
async def post_category_unarchive(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    category_id: Annotated[UUID, Path(alias="categoryId")],
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CategoryResponse:
    """Unarchive one Category idempotently."""

    return await _change_category_status(
        ledger_id=ledger_id,
        category_id=category_id,
        actor=actor,
        session=session,
        status="active",
    )


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
