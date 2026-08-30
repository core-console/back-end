"""Finance HTTP routes."""

from collections.abc import Awaitable
from decimal import Decimal
from http import HTTPStatus
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.database.dependencies import get_session
from core_console.modules.finance.currencies import SUPPORTED_CURRENCIES
from core_console.modules.finance.history_schemas import (
    TransactionHistoryFilters,
    TransactionHistoryPageResponse,
)
from core_console.modules.finance.models import FinanceCategory, FinanceLedger
from core_console.modules.finance.money import CurrencyCode, Money
from core_console.modules.finance.overview_schemas import (
    DayActivityByCurrencyResponse,
    FinanceOverviewDayResponse,
    FinanceOverviewMonth,
    FinanceOverviewResponse,
    FinancialPositionByCurrencyResponse,
    IncomeExpenseByCurrencyResponse,
    TransactionCountByKindResponse,
)
from core_console.modules.finance.queries import (
    FinanceAccountBalance,
    FinanceTransactionDetail,
    list_finance_ledgers,
)
from core_console.modules.finance.schemas import (
    AccountReferenceResponse,
    AccountResponse,
    BalanceAdjustmentContextResponse,
    BalanceAdjustmentCreatedResultResponse,
    BalanceAdjustmentNoChangeResultResponse,
    BalanceAdjustmentRemovedResultResponse,
    BalanceAdjustmentResultResponse,
    BalanceAdjustmentTransactionResponse,
    BalanceAdjustmentUpdatedResultResponse,
    CategoryAllocationResponse,
    CategoryReferenceResponse,
    CategoryResponse,
    CorrectAccountSemanticsRequest,
    CreateAccountRequest,
    CreateBalanceAdjustmentRequest,
    CreateCategoryRequest,
    CreateFinanceTransactionRequest,
    CreateLedgerRequest,
    CurrencyResponse,
    ExpenseTransactionResponse,
    FinanceRequestDate,
    FinanceTransactionResponse,
    IncomeTransactionResponse,
    InternalTransferTransactionResponse,
    LedgerResponse,
    MoneyResponse,
    ReplaceBalanceAdjustmentRequest,
    ReplaceBalanceAdjustmentResultResponse,
    ReplaceFinanceTransactionRequest,
    UpdateCategoryRequest,
    UpdateFinanceAccountRequest,
    UpdateLedgerRequest,
)
from core_console.modules.finance.service import (
    BalanceAdjustmentResult,
    FinanceAccountArchivedError,
    FinanceAccountBalanceChangedError,
    FinanceAccountNotFoundError,
    FinanceAccountSemanticsChangedError,
    FinanceAccountSemanticsLockedError,
    FinanceCategoryArchivedError,
    FinanceCategoryNameConflictError,
    FinanceCategoryNotFoundError,
    FinanceLedgerNameConflictError,
    FinanceLedgerNotFoundError,
    FinanceTransactionKindImmutableError,
    FinanceTransactionNotFoundError,
    InvalidFinanceAccountMoneyError,
    InvalidFinanceAccountNameError,
    InvalidFinanceAccountTrackingStartDateError,
    InvalidFinanceCategoryNameError,
    InvalidFinanceLedgerNameError,
    InvalidFinanceTransactionError,
    archive_finance_account,
    archive_finance_category,
    correct_finance_account_semantics,
    create_balance_adjustment,
    create_finance_account,
    create_finance_category,
    create_finance_ledger,
    create_finance_transaction,
    create_internal_transfer_transaction,
    delete_finance_transaction,
    get_balance_adjustment_context,
    get_finance_overview,
    get_finance_transaction,
    list_finance_accounts,
    list_finance_categories_for_ledger,
    list_finance_transactions,
    replace_balance_adjustment,
    replace_finance_transaction,
    replace_internal_transfer_transaction,
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

type _FinanceApplicationError = (
    FinanceAccountArchivedError
    | FinanceAccountBalanceChangedError
    | FinanceAccountNotFoundError
    | FinanceAccountSemanticsLockedError
    | FinanceAccountSemanticsChangedError
    | FinanceCategoryArchivedError
    | FinanceCategoryNameConflictError
    | FinanceCategoryNotFoundError
    | FinanceLedgerNameConflictError
    | FinanceLedgerNotFoundError
    | FinanceTransactionNotFoundError
    | FinanceTransactionKindImmutableError
    | InvalidFinanceAccountMoneyError
    | InvalidFinanceAccountNameError
    | InvalidFinanceAccountTrackingStartDateError
    | InvalidFinanceCategoryNameError
    | InvalidFinanceLedgerNameError
    | InvalidFinanceTransactionError
)


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


def _to_transaction_response(
    detail: FinanceTransactionDetail,
) -> FinanceTransactionResponse:
    """Build the complete closed projection before a create commit."""

    transaction = detail.transaction
    if transaction.kind == "balance_adjustment":
        account = detail.account
        return BalanceAdjustmentTransactionResponse(
            id=transaction.id,
            ledgerId=transaction.ledger_id,
            kind="balanceAdjustment",
            transactionDate=transaction.transaction_date,
            note=transaction.note,
            account=AccountReferenceResponse(
                id=account.id,
                name=account.name,
                status=cast(Literal["active", "archived"], account.status),
            ),
            correctionDelta=_to_money_response(detail.movement.amount, detail.movement.currency),
        )
    if transaction.kind == "internal_transfer":
        movement_accounts = {
            item.movement.role: (item.movement, item.account) for item in detail.movement_details
        }
        source_movement, source_account = movement_accounts["source"]
        destination_movement, destination_account = movement_accounts["destination"]
        return InternalTransferTransactionResponse(
            id=transaction.id,
            ledgerId=transaction.ledger_id,
            kind="internalTransfer",
            transactionDate=transaction.transaction_date,
            note=transaction.note,
            sourceAccount=AccountReferenceResponse(
                id=source_account.id,
                name=source_account.name,
                status=cast(Literal["active", "archived"], source_account.status),
            ),
            sourceAmount=_to_money_response(abs(source_movement.amount), source_movement.currency),
            destinationAccount=AccountReferenceResponse(
                id=destination_account.id,
                name=destination_account.name,
                status=cast(Literal["active", "archived"], destination_account.status),
            ),
            destinationAmount=_to_money_response(
                abs(destination_movement.amount), destination_movement.currency
            ),
        )
    account = detail.account
    allocation = detail.allocation
    account_reference = AccountReferenceResponse(
        id=account.id,
        name=account.name,
        status=cast(Literal["active", "archived"], account.status),
    )
    category_reference = (
        CategoryReferenceResponse(
            id=detail.category.id,
            name=detail.category.name,
            status=cast(Literal["active", "archived"], detail.category.status),
        )
        if detail.category is not None
        else None
    )
    economic_amount = _to_money_response(allocation.amount, allocation.currency)
    category_allocations = [
        CategoryAllocationResponse(
            amount=economic_amount,
            category=category_reference,
        )
    ]
    if transaction.kind == "income":
        return IncomeTransactionResponse(
            id=transaction.id,
            ledgerId=transaction.ledger_id,
            kind="income",
            transactionDate=transaction.transaction_date,
            note=transaction.note,
            account=account_reference,
            economicAmount=economic_amount,
            categoryAllocations=category_allocations,
        )
    return ExpenseTransactionResponse(
        id=transaction.id,
        ledgerId=transaction.ledger_id,
        kind="expense",
        transactionDate=transaction.transaction_date,
        note=transaction.note,
        account=account_reference,
        economicAmount=economic_amount,
        categoryAllocations=category_allocations,
    )


def _to_balance_adjustment_result_response(
    result: BalanceAdjustmentResult[FinanceTransactionDetail],
) -> BalanceAdjustmentResultResponse:
    """Validate the complete Adjustment command result before commit."""

    detail = result.transaction
    if result.outcome == "noChange":
        if detail is not None:
            raise RuntimeError("A no-change Balance Adjustment cannot contain a Transaction.")
        return BalanceAdjustmentResultResponse(
            BalanceAdjustmentNoChangeResultResponse(outcome="noChange", transaction=None)
        )
    if result.outcome != "created":
        raise RuntimeError("Create Balance Adjustment returned an invalid outcome.")
    if detail is None:
        raise RuntimeError("A created Balance Adjustment must contain a Transaction.")
    transaction = _to_transaction_response(detail)
    if not isinstance(transaction, BalanceAdjustmentTransactionResponse):
        raise RuntimeError("Balance Adjustment projection returned the wrong Transaction kind.")
    return BalanceAdjustmentResultResponse(
        BalanceAdjustmentCreatedResultResponse(outcome="created", transaction=transaction)
    )


def _to_replace_balance_adjustment_result_response(
    result: BalanceAdjustmentResult[FinanceTransactionDetail],
) -> ReplaceBalanceAdjustmentResultResponse:
    """Validate the complete replacement result before commit."""

    detail = result.transaction
    if result.outcome == "removed":
        if detail is not None:
            raise RuntimeError("A removed Balance Adjustment cannot contain a Transaction.")
        return ReplaceBalanceAdjustmentResultResponse(
            BalanceAdjustmentRemovedResultResponse(outcome="removed", transaction=None)
        )
    if result.outcome != "updated":
        raise RuntimeError("Replace Balance Adjustment returned an invalid outcome.")
    if detail is None:
        raise RuntimeError("An updated Balance Adjustment must contain a Transaction.")
    transaction = _to_transaction_response(detail)
    if not isinstance(transaction, BalanceAdjustmentTransactionResponse):
        raise RuntimeError("Balance Adjustment projection returned the wrong Transaction kind.")
    return ReplaceBalanceAdjustmentResultResponse(
        BalanceAdjustmentUpdatedResultResponse(outcome="updated", transaction=transaction)
    )


async def _run_finance_workflow[Result](workflow: Awaitable[Result]) -> Result:
    """Translate known Finance failures at the Finance HTTP boundary."""

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
    except (
        FinanceAccountArchivedError,
        FinanceAccountBalanceChangedError,
        FinanceAccountNotFoundError,
        FinanceAccountSemanticsLockedError,
        FinanceAccountSemanticsChangedError,
        FinanceCategoryArchivedError,
        FinanceCategoryNameConflictError,
        FinanceCategoryNotFoundError,
        FinanceLedgerNameConflictError,
        FinanceLedgerNotFoundError,
        FinanceTransactionNotFoundError,
        FinanceTransactionKindImmutableError,
        InvalidFinanceAccountMoneyError,
        InvalidFinanceAccountNameError,
        InvalidFinanceAccountTrackingStartDateError,
        InvalidFinanceCategoryNameError,
        InvalidFinanceLedgerNameError,
        InvalidFinanceTransactionError,
    ) as exc:
        raise _finance_problem_for(exc) from None


def _finance_problem_for(error: _FinanceApplicationError) -> ApplicationProblem:
    """Build the stable Problem Details result for one known Finance failure."""

    if isinstance(
        error,
        (
            InvalidFinanceAccountMoneyError,
            InvalidFinanceAccountNameError,
            InvalidFinanceAccountTrackingStartDateError,
            InvalidFinanceCategoryNameError,
            InvalidFinanceLedgerNameError,
            InvalidFinanceTransactionError,
        ),
    ):
        return ApplicationProblem(
            status=HTTPStatus.UNPROCESSABLE_ENTITY,
            title="Unprocessable Entity",
            detail=str(error),
            code="validation_error",
        )
    if isinstance(error, FinanceLedgerNotFoundError):
        return ApplicationProblem(
            status=HTTPStatus.NOT_FOUND,
            title="Not Found",
            detail="The requested Finance Ledger does not exist.",
            code="finance_ledger_not_found",
        )
    if isinstance(error, FinanceAccountNotFoundError):
        return ApplicationProblem(
            status=HTTPStatus.NOT_FOUND,
            title="Not Found",
            detail="The requested Finance Account does not exist.",
            code="finance_account_not_found",
        )
    if isinstance(error, FinanceAccountSemanticsLockedError):
        return ApplicationProblem(
            status=HTTPStatus.CONFLICT,
            title="Conflict",
            detail=(
                "Account Nature or Currency cannot change because Opening Balance is "
                "non-zero or Transaction history exists."
            ),
            code="finance_account_semantics_locked",
        )
    if isinstance(error, FinanceAccountBalanceChangedError):
        return ApplicationProblem(
            status=HTTPStatus.CONFLICT,
            title="Conflict",
            detail="The Finance Account balance changed; refresh the adjustment context.",
            code="account_balance_changed",
        )
    if isinstance(error, FinanceAccountSemanticsChangedError):
        return ApplicationProblem(
            status=HTTPStatus.CONFLICT,
            title="Conflict",
            detail="The Finance Account Nature changed; refresh the adjustment context.",
            code="finance_account_semantics_changed",
        )
    if isinstance(error, FinanceCategoryNotFoundError):
        return ApplicationProblem(
            status=HTTPStatus.NOT_FOUND,
            title="Not Found",
            detail="The requested Finance Category does not exist.",
            code="finance_category_not_found",
        )
    if isinstance(error, FinanceTransactionNotFoundError):
        return ApplicationProblem(
            status=HTTPStatus.NOT_FOUND,
            title="Not Found",
            detail="The requested Finance Transaction does not exist.",
            code="finance_transaction_not_found",
        )
    if isinstance(error, FinanceTransactionKindImmutableError):
        return ApplicationProblem(
            status=HTTPStatus.CONFLICT,
            title="Conflict",
            detail="The Finance Transaction kind cannot be changed by this operation.",
            code="finance_transaction_kind_immutable",
        )
    if isinstance(error, FinanceLedgerNameConflictError):
        return ApplicationProblem(
            status=HTTPStatus.CONFLICT,
            title="Conflict",
            detail="A Finance Ledger with this name already exists.",
            code="finance_ledger_name_conflict",
        )
    if isinstance(error, FinanceCategoryNameConflictError):
        return ApplicationProblem(
            status=HTTPStatus.CONFLICT,
            title="Conflict",
            detail="A Finance Category with this name already exists.",
            code="finance_category_name_conflict",
        )
    if isinstance(error, FinanceAccountArchivedError):
        return ApplicationProblem(
            status=HTTPStatus.CONFLICT,
            title="Conflict",
            detail="An archived Finance Account cannot receive a new Transaction.",
            code="finance_account_archived",
        )
    if isinstance(error, FinanceCategoryArchivedError):
        return ApplicationProblem(
            status=HTTPStatus.CONFLICT,
            title="Conflict",
            detail="An archived Finance Category cannot classify a new Transaction.",
            code="finance_category_archived",
        )
    raise AssertionError(f"Unhandled Finance application error: {type(error).__name__}")


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

    balances = await _run_finance_workflow(
        list_finance_accounts(
            session,
            owner_id=actor.id,
            ledger_id=ledger_id,
        )
    )
    return [_to_account_response(balance) for balance in balances]


@router.get(
    "/ledgers/{ledgerId}/overview",
    operation_id="getFinanceOverview",
    summary="Get the Finance Overview",
    description=(
        "Returns present Account balances and currency-separated selected-month activity."
    ),
    response_model=FinanceOverviewResponse,
    responses={
        403: {"model": ProblemDetails, "description": "Access is denied."},
        404: {"model": ProblemDetails, "description": "The Ledger does not exist."},
        422: {"model": ProblemDetails, "description": "The request is invalid."},
        500: {"model": ProblemDetails, "description": "An unexpected error occurred."},
        503: {"model": ProblemDetails, "description": "PostgreSQL is unavailable."},
    },
    openapi_extra={"security": []},
)
async def get_overview(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    month: Annotated[FinanceOverviewMonth, Query()],
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> FinanceOverviewResponse:
    """Read one owned Ledger's present position and selected-month activity."""

    overview = await _run_finance_workflow(
        get_finance_overview(
            session,
            owner_id=actor.id,
            ledger_id=ledger_id,
            month=month,
        )
    )
    return FinanceOverviewResponse(
        ledger=_to_ledger_response(overview.ledger),
        month=overview.month,
        accounts=[_to_account_response(account) for account in overview.accounts],
        financialPositionByCurrency=[
            FinancialPositionByCurrencyResponse(
                currency=position.currency,
                assetTotal=_to_money_response(position.asset_total, position.currency),
                liabilityTotal=_to_money_response(position.liability_total, position.currency),
                netPosition=_to_money_response(position.net_position, position.currency),
            )
            for position in overview.financial_positions
        ],
        monthSummaryByCurrency=[
            IncomeExpenseByCurrencyResponse(
                currency=summary.currency,
                income=_to_money_response(summary.income, summary.currency),
                expense=_to_money_response(summary.expense, summary.currency),
                net=_to_money_response(summary.net, summary.currency),
            )
            for summary in overview.month_summaries
        ],
        days=[
            FinanceOverviewDayResponse(
                date=day.date,
                transactionCount=day.transaction_count,
                transactionCountByKind=TransactionCountByKindResponse(
                    income=day.transaction_counts.income,
                    expense=day.transaction_counts.expense,
                    internalTransfer=day.transaction_counts.internal_transfer,
                    balanceAdjustment=day.transaction_counts.balance_adjustment,
                ),
                activityByCurrency=[
                    DayActivityByCurrencyResponse(
                        currency=activity.currency,
                        income=_to_money_response(activity.income, activity.currency),
                        expense=_to_money_response(activity.expense, activity.currency),
                        net=_to_money_response(activity.net, activity.currency),
                        transactionCount=activity.transaction_count,
                    )
                    for activity in day.activity_by_currency
                ],
            )
            for day in overview.days
        ],
    )


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

    ledger = await _run_finance_workflow(
        create_finance_ledger(session, owner_id=actor.id, name=request.name)
    )
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
    return _to_account_response(balance)


@router.patch(
    "/ledgers/{ledgerId}/accounts/{accountId}",
    operation_id="updateFinanceAccount",
    summary="Update a Finance Account",
    description=(
        "Updates ordinary mutable properties or corrects unlocked Nature and Currency "
        "of one owned Account."
    ),
    response_model=AccountResponse,
    responses={
        403: {"model": ProblemDetails, "description": "Access is denied."},
        404: {"model": ProblemDetails, "description": "The resource does not exist."},
        409: {"model": ProblemDetails, "description": "Account semantics are locked."},
        422: {"model": ProblemDetails, "description": "The request is invalid."},
        500: {"model": ProblemDetails, "description": "An unexpected error occurred."},
        503: {"model": ProblemDetails, "description": "PostgreSQL is unavailable."},
    },
    openapi_extra={"security": []},
)
async def patch_account(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    account_id: Annotated[UUID, Path(alias="accountId")],
    request: UpdateFinanceAccountRequest,
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AccountResponse:
    """Apply one closed ordinary-edit or semantic-correction Account workflow."""

    if isinstance(request, CorrectAccountSemanticsRequest):
        balance = await _run_finance_workflow(
            correct_finance_account_semantics(
                session,
                owner_id=actor.id,
                ledger_id=ledger_id,
                account_id=account_id,
                nature=request.nature,
                currency=request.currency,
            )
        )
    else:
        fields = request.model_fields_set
        opening_balance = (
            request.opening_balance.to_money() if "opening_balance" in fields else None
        )
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
    balance = await _run_finance_workflow(
        workflow(
            session,
            owner_id=actor.id,
            ledger_id=ledger_id,
            account_id=account_id,
        )
    )
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

    categories = await _run_finance_workflow(
        list_finance_categories_for_ledger(
            session,
            owner_id=actor.id,
            ledger_id=ledger_id,
        )
    )
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

    category = await _run_finance_workflow(
        create_finance_category(
            session,
            owner_id=actor.id,
            ledger_id=ledger_id,
            name=request.name,
        )
    )
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
    category = await _run_finance_workflow(
        update_finance_category(
            session,
            owner_id=actor.id,
            ledger_id=ledger_id,
            category_id=category_id,
            name=name,
        )
    )
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
    category = await _run_finance_workflow(
        workflow(
            session,
            owner_id=actor.id,
            ledger_id=ledger_id,
            category_id=category_id,
        )
    )
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
    ledger = await _run_finance_workflow(
        update_finance_ledger(
            session,
            owner_id=actor.id,
            ledger_id=ledger_id,
            name=name,
        )
    )
    return _to_ledger_response(ledger)


@router.get(
    "/ledgers/{ledgerId}/accounts/{accountId}/balance-adjustment-context",
    operation_id="getBalanceAdjustmentContext",
    summary="Get Balance Adjustment context",
    response_model=BalanceAdjustmentContextResponse,
    responses={
        403: {"model": ProblemDetails, "description": "Access is denied."},
        404: {"model": ProblemDetails, "description": "The resource does not exist."},
        409: {"model": ProblemDetails, "description": "The Account is archived."},
        422: {"model": ProblemDetails, "description": "The request is invalid."},
        500: {"model": ProblemDetails, "description": "An unexpected error occurred."},
        503: {"model": ProblemDetails, "description": "PostgreSQL is unavailable."},
    },
    openapi_extra={"security": []},
)
async def get_adjustment_context(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    account_id: Annotated[UUID, Path(alias="accountId")],
    transaction_date: Annotated[FinanceRequestDate, Query(alias="transactionDate")],
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    replacing_transaction_id: Annotated[
        UUID | None,
        Query(alias="replacingTransactionId"),
    ] = None,
) -> BalanceAdjustmentContextResponse:
    """Return authoritative date-bounded Balance Adjustment command inputs."""

    balance = await _run_finance_workflow(
        get_balance_adjustment_context(
            session,
            owner_id=actor.id,
            ledger_id=ledger_id,
            account_id=account_id,
            transaction_date=transaction_date,
            replacing_transaction_id=replacing_transaction_id,
        )
    )
    account = balance.account
    return BalanceAdjustmentContextResponse(
        account=AccountReferenceResponse(
            id=account.id,
            name=account.name,
            status=cast(Literal["active", "archived"], account.status),
        ),
        transactionDate=transaction_date,
        derivedComparisonBalance=_to_money_response(balance.current_balance, account.currency),
        accountNature=cast(Literal["asset", "liability"], account.nature),
    )


@router.post(
    "/ledgers/{ledgerId}/balance-adjustments",
    operation_id="createBalanceAdjustment",
    summary="Create a Balance Adjustment",
    response_model=BalanceAdjustmentResultResponse,
    responses={
        403: {"model": ProblemDetails, "description": "Access is denied."},
        404: {"model": ProblemDetails, "description": "The resource does not exist."},
        409: {"model": ProblemDetails, "description": "The context is stale or archived."},
        422: {"model": ProblemDetails, "description": "The request is invalid."},
        500: {"model": ProblemDetails, "description": "An unexpected error occurred."},
        503: {"model": ProblemDetails, "description": "PostgreSQL is unavailable."},
    },
    openapi_extra={"security": []},
)
async def post_balance_adjustment(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    request: CreateBalanceAdjustmentRequest,
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> BalanceAdjustmentResultResponse:
    """Create only the required non-zero account-relative correction delta."""

    return await _run_finance_workflow(
        create_balance_adjustment(
            session,
            owner_id=actor.id,
            ledger_id=ledger_id,
            account_id=request.account_id,
            transaction_date=request.transaction_date,
            expected_derived_balance=request.expected_derived_balance.to_money(),
            expected_account_nature=request.expected_account_nature,
            target_balance=request.target_balance.to_money(),
            note=request.note,
            project=_to_balance_adjustment_result_response,
        )
    )


@router.put(
    "/ledgers/{ledgerId}/balance-adjustments/{transactionId}",
    operation_id="replaceBalanceAdjustment",
    summary="Replace or remove a Balance Adjustment",
    response_model=ReplaceBalanceAdjustmentResultResponse,
    responses={
        403: {"model": ProblemDetails, "description": "Access is denied."},
        404: {"model": ProblemDetails, "description": "The resource does not exist."},
        409: {"model": ProblemDetails, "description": "The context is stale or immutable."},
        422: {"model": ProblemDetails, "description": "The request is invalid."},
        500: {"model": ProblemDetails, "description": "An unexpected error occurred."},
        503: {"model": ProblemDetails, "description": "PostgreSQL is unavailable."},
    },
    openapi_extra={"security": []},
)
async def put_balance_adjustment(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    transaction_id: Annotated[UUID, Path(alias="transactionId")],
    request: ReplaceBalanceAdjustmentRequest,
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ReplaceBalanceAdjustmentResultResponse:
    """Replace an Adjustment from a fresh target, or remove it at zero delta."""

    return await _run_finance_workflow(
        replace_balance_adjustment(
            session,
            owner_id=actor.id,
            ledger_id=ledger_id,
            transaction_id=transaction_id,
            account_id=request.account_id,
            transaction_date=request.transaction_date,
            expected_derived_balance=request.expected_derived_balance.to_money(),
            expected_account_nature=request.expected_account_nature,
            target_balance=request.target_balance.to_money(),
            note=request.note,
            project=_to_replace_balance_adjustment_result_response,
        )
    )


@router.post(
    "/ledgers/{ledgerId}/transactions",
    operation_id="createFinanceTransaction",
    summary="Create a Finance Transaction",
    status_code=HTTPStatus.CREATED,
    response_model=FinanceTransactionResponse,
    responses={
        403: {"model": ProblemDetails, "description": "Access is denied."},
        404: {"model": ProblemDetails, "description": "The resource does not exist."},
        409: {"model": ProblemDetails, "description": "The resource is archived."},
        422: {"model": ProblemDetails, "description": "The request is invalid."},
        500: {"model": ProblemDetails, "description": "An unexpected error occurred."},
        503: {"model": ProblemDetails, "description": "PostgreSQL is unavailable."},
    },
    openapi_extra={"security": []},
)
async def post_transaction(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    request: CreateFinanceTransactionRequest,
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> FinanceTransactionResponse:
    """Create one supported Finance Transaction."""

    if request.kind == "internalTransfer":
        return await _run_finance_workflow(
            create_internal_transfer_transaction(
                session,
                owner_id=actor.id,
                ledger_id=ledger_id,
                source_account_id=request.source_account_id,
                destination_account_id=request.destination_account_id,
                transaction_date=request.transaction_date,
                amount=request.amount.to_money(),
                note=request.note,
                project=_to_transaction_response,
            )
        )
    allocation = request.category_allocations[0]
    return await _run_finance_workflow(
        create_finance_transaction(
            session,
            owner_id=actor.id,
            ledger_id=ledger_id,
            kind=request.kind,
            account_id=request.account_id,
            transaction_date=request.transaction_date,
            economic_amount=request.economic_amount.to_money(),
            allocation_amount=allocation.amount.to_money(),
            category_id=allocation.category_id,
            note=request.note,
            project=_to_transaction_response,
        )
    )


@router.get(
    "/ledgers/{ledgerId}/transactions",
    operation_id="listFinanceTransactions",
    summary="List Finance Transactions",
    response_model=TransactionHistoryPageResponse,
    responses={
        403: {"model": ProblemDetails, "description": "Access is denied."},
        404: {"model": ProblemDetails, "description": "The resource does not exist."},
        422: {"model": ProblemDetails, "description": "The query is invalid."},
        500: {"model": ProblemDetails, "description": "An unexpected error occurred."},
        503: {"model": ProblemDetails, "description": "PostgreSQL is unavailable."},
    },
    openapi_extra={"security": []},
)
async def list_transactions(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    filters: Annotated[TransactionHistoryFilters, Query()],
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TransactionHistoryPageResponse:
    """Browse one owned Ledger's deterministic Transaction history."""

    page = await _run_finance_workflow(
        list_finance_transactions(
            session,
            owner_id=actor.id,
            ledger_id=ledger_id,
            from_date=filters.from_date,
            to_date=filters.to_date,
            account_id=filters.account_id,
            kind=filters.kind,
            category_id=filters.category_id,
            uncategorized=filters.uncategorized is True,
            cursor=filters.cursor,
            page_size=filters.page_size,
        )
    )
    return TransactionHistoryPageResponse(
        items=[_to_transaction_response(detail) for detail in page.items],
        nextCursor=page.next_cursor,
    )


@router.get(
    "/ledgers/{ledgerId}/transactions/{transactionId}",
    operation_id="getFinanceTransaction",
    summary="Get a Finance Transaction",
    response_model=FinanceTransactionResponse,
    responses={
        403: {"model": ProblemDetails, "description": "Access is denied."},
        404: {"model": ProblemDetails, "description": "The resource does not exist."},
        422: {"model": ProblemDetails, "description": "The path is invalid."},
        500: {"model": ProblemDetails, "description": "An unexpected error occurred."},
        503: {"model": ProblemDetails, "description": "PostgreSQL is unavailable."},
    },
    openapi_extra={"security": []},
)
async def get_transaction(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    transaction_id: Annotated[UUID, Path(alias="transactionId")],
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> FinanceTransactionResponse:
    """Read one supported Finance Transaction in an owned Ledger."""

    detail = await _run_finance_workflow(
        get_finance_transaction(
            session,
            owner_id=actor.id,
            ledger_id=ledger_id,
            transaction_id=transaction_id,
        )
    )
    return _to_transaction_response(detail)


@router.put(
    "/ledgers/{ledgerId}/transactions/{transactionId}",
    operation_id="replaceFinanceTransaction",
    summary="Replace a Finance Transaction",
    response_model=FinanceTransactionResponse,
    responses={
        403: {"model": ProblemDetails, "description": "Access is denied."},
        404: {"model": ProblemDetails, "description": "The resource does not exist."},
        409: {"model": ProblemDetails, "description": "The kind is immutable or archived."},
        422: {"model": ProblemDetails, "description": "The request is invalid."},
        500: {"model": ProblemDetails, "description": "An unexpected error occurred."},
        503: {"model": ProblemDetails, "description": "PostgreSQL is unavailable."},
    },
    openapi_extra={"security": []},
)
async def put_transaction(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    transaction_id: Annotated[UUID, Path(alias="transactionId")],
    request: ReplaceFinanceTransactionRequest,
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> FinanceTransactionResponse:
    """Completely replace one same-kind ordinary Finance Transaction."""

    if request.kind == "internalTransfer":
        return await _run_finance_workflow(
            replace_internal_transfer_transaction(
                session,
                owner_id=actor.id,
                ledger_id=ledger_id,
                transaction_id=transaction_id,
                source_account_id=request.source_account_id,
                destination_account_id=request.destination_account_id,
                transaction_date=request.transaction_date,
                amount=request.amount.to_money(),
                note=request.note,
                project=_to_transaction_response,
            )
        )
    allocation = request.category_allocations[0]
    return await _run_finance_workflow(
        replace_finance_transaction(
            session,
            owner_id=actor.id,
            ledger_id=ledger_id,
            transaction_id=transaction_id,
            kind=request.kind,
            account_id=request.account_id,
            transaction_date=request.transaction_date,
            economic_amount=request.economic_amount.to_money(),
            allocation_amount=allocation.amount.to_money(),
            category_id=allocation.category_id,
            note=request.note,
            project=_to_transaction_response,
        )
    )


@router.delete(
    "/ledgers/{ledgerId}/transactions/{transactionId}",
    operation_id="deleteFinanceTransaction",
    summary="Delete a Finance Transaction",
    status_code=HTTPStatus.NO_CONTENT,
    response_model=None,
    responses={
        403: {"model": ProblemDetails, "description": "Access is denied."},
        404: {"model": ProblemDetails, "description": "The resource does not exist."},
        422: {"model": ProblemDetails, "description": "The path is invalid."},
        500: {"model": ProblemDetails, "description": "An unexpected error occurred."},
        503: {"model": ProblemDetails, "description": "PostgreSQL is unavailable."},
    },
    openapi_extra={"security": []},
)
async def delete_transaction(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    transaction_id: Annotated[UUID, Path(alias="transactionId")],
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Delete one complete Finance Transaction aggregate of any kind."""

    await _run_finance_workflow(
        delete_finance_transaction(
            session,
            owner_id=actor.id,
            ledger_id=ledger_id,
            transaction_id=transaction_id,
        )
    )
