"""Public HTTP schemas for the Finance module."""

from datetime import date
from re import fullmatch
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    RootModel,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from core_console.modules.finance.money import CurrencyCode, InvalidMoneyError, Money


def _validate_exact_calendar_date(value: object) -> object:
    """Reject datetime coercion and require the public ISO calendar-date shape."""

    if type(value) is date or (
        isinstance(value, str) and fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value)
    ):
        return value
    raise PydanticCustomError(
        "date_format",
        "Date must use exact ISO YYYY-MM-DD format.",
    )


type FinanceRequestDate = Annotated[date, BeforeValidator(_validate_exact_calendar_date)]


class CurrencyResponse(BaseModel):
    """One backend-supported currency and its decimal scale."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    code: CurrencyCode
    minor_unit: int = Field(alias="minorUnit", ge=0)


class LedgerResponse(BaseModel):
    """Closed public projection of one Finance Ledger."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    name: str


class MoneyResponse(BaseModel):
    """Closed public projection of exact Finance Money."""

    model_config = ConfigDict(extra="forbid")

    amount: str
    currency: CurrencyCode


class AccountResponse(BaseModel):
    """Closed public projection of one Finance Account."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: UUID
    name: str
    nature: Literal["asset", "liability"]
    currency: CurrencyCode
    opening_balance: MoneyResponse = Field(alias="openingBalance")
    tracking_start_date: date = Field(alias="trackingStartDate")
    current_balance: MoneyResponse = Field(alias="currentBalance")
    status: Literal["active", "archived"]


class CategoryResponse(BaseModel):
    """Closed public projection of one neutral Finance Category."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    name: str
    status: Literal["active", "archived"]


class AccountReferenceResponse(BaseModel):
    """Current lightweight Account reference embedded in a Transaction."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    name: str
    status: Literal["active", "archived"]


class CategoryReferenceResponse(BaseModel):
    """Current lightweight Category reference embedded in an Allocation."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    name: str
    status: Literal["active", "archived"]


class CategoryAllocationResponse(BaseModel):
    """One exact classified or Uncategorized Economic Amount portion."""

    model_config = ConfigDict(extra="forbid")

    amount: MoneyResponse
    category: CategoryReferenceResponse | None


class _OrdinaryTransactionResponse(BaseModel):
    """Closed common projection for Income and Expense."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: UUID
    ledger_id: UUID = Field(alias="ledgerId")
    transaction_date: date = Field(alias="transactionDate")
    note: str | None = Field(max_length=500)
    account: AccountReferenceResponse
    economic_amount: MoneyResponse = Field(alias="economicAmount")
    category_allocations: list[CategoryAllocationResponse] = Field(
        alias="categoryAllocations",
        min_length=1,
        max_length=1,
    )


class IncomeTransactionResponse(_OrdinaryTransactionResponse):
    """Closed public Income projection."""

    kind: Literal["income"]


class ExpenseTransactionResponse(_OrdinaryTransactionResponse):
    """Closed public Expense projection."""

    kind: Literal["expense"]


class InternalTransferTransactionResponse(BaseModel):
    """Closed public same-currency Internal Transfer projection."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: UUID
    ledger_id: UUID = Field(alias="ledgerId")
    kind: Literal["internalTransfer"]
    transaction_date: date = Field(alias="transactionDate")
    note: str | None = Field(max_length=500)
    source_account: AccountReferenceResponse = Field(alias="sourceAccount")
    source_amount: MoneyResponse = Field(alias="sourceAmount")
    destination_account: AccountReferenceResponse = Field(alias="destinationAccount")
    destination_amount: MoneyResponse = Field(alias="destinationAmount")


class BalanceAdjustmentTransactionResponse(BaseModel):
    """Closed public Balance Adjustment correction projection."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: UUID
    ledger_id: UUID = Field(alias="ledgerId")
    kind: Literal["balanceAdjustment"]
    transaction_date: date = Field(alias="transactionDate")
    note: str | None = Field(max_length=500)
    account: AccountReferenceResponse
    correction_delta: MoneyResponse = Field(alias="correctionDelta")


type FinanceTransactionResponse = Annotated[
    IncomeTransactionResponse
    | ExpenseTransactionResponse
    | InternalTransferTransactionResponse
    | BalanceAdjustmentTransactionResponse,
    Field(discriminator="kind"),
]


class BalanceAdjustmentContextResponse(BaseModel):
    """Authoritative inputs for one stale-safe target-balance command."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    account: AccountReferenceResponse
    transaction_date: date = Field(alias="transactionDate")
    derived_comparison_balance: MoneyResponse = Field(alias="derivedComparisonBalance")
    account_nature: Literal["asset", "liability"] = Field(alias="accountNature")


class BalanceAdjustmentCreatedResultResponse(BaseModel):
    """A command result containing the newly created Adjustment."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["created"]
    transaction: BalanceAdjustmentTransactionResponse


class _RequestModel(BaseModel):
    """Accept only declared public Finance JSON fields."""

    model_config = ConfigDict(
        extra="forbid",
        validate_by_alias=True,
        validate_by_name=False,
    )


class BalanceAdjustmentNoChangeResultResponse(BaseModel):
    """A write-free command result with no durable Transaction."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["noChange"]
    transaction: None


class BalanceAdjustmentResultResponse(
    RootModel[
        Annotated[
            BalanceAdjustmentCreatedResultResponse | BalanceAdjustmentNoChangeResultResponse,
            Field(discriminator="outcome"),
        ]
    ]
):
    """Exact outcome-discriminated Balance Adjustment command result."""


class CreateLedgerRequest(_RequestModel):
    """Explicit first or additional Ledger creation."""

    name: str = Field(max_length=100)


class UpdateLedgerRequest(_RequestModel):
    """Name-only partial Ledger update."""

    name: str = Field(default_factory=str, max_length=100)


class CreateCategoryRequest(_RequestModel):
    """Explicit creation state for one Finance Category."""

    name: str = Field(max_length=100)


class UpdateCategoryRequest(_RequestModel):
    """Name-only partial Category update."""

    name: str = Field(default_factory=str, max_length=100)


class MoneyRequest(_RequestModel):
    """Exact decimal-string Money supplied by a Finance caller."""

    amount: str = Field(strict=True)
    currency: CurrencyCode

    @model_validator(mode="after")
    def validate_exact_amount(self) -> Self:
        try:
            Money.parse(amount=self.amount, currency=self.currency)
        except InvalidMoneyError as exc:
            raise PydanticCustomError("value_error", str(exc)) from None
        return self

    def to_money(self) -> Money:
        """Return the validated exact application value."""

        return Money.parse(amount=self.amount, currency=self.currency)


class CreateAccountRequest(_RequestModel):
    """Explicit creation state for one Finance Account."""

    name: str = Field(max_length=100)
    nature: Literal["asset", "liability"]
    currency: CurrencyCode
    opening_balance: MoneyRequest = Field(alias="openingBalance")
    tracking_start_date: FinanceRequestDate = Field(alias="trackingStartDate")

    @model_validator(mode="after")
    def validate_opening_balance_currency(self) -> Self:
        if self.opening_balance.currency != self.currency:
            raise PydanticCustomError(
                "value_error",
                "Opening Balance currency must match the Account currency.",
            )
        return self


def _default_money_request() -> MoneyRequest:
    """Supply an ignored valid value for an omitted PATCH field."""

    return MoneyRequest(amount="0", currency="CNY")


class UpdateAccountRequest(_RequestModel):
    """Ordinary mutable Account fields using omitted-field semantics."""

    name: str = Field(default_factory=str, max_length=100)
    opening_balance: MoneyRequest = Field(
        default_factory=_default_money_request,
        alias="openingBalance",
    )
    tracking_start_date: FinanceRequestDate = Field(
        default_factory=lambda: date.min,
        alias="trackingStartDate",
    )


class CorrectAccountSemanticsRequest(_RequestModel):
    """Restricted Nature and Currency correction fields."""

    model_config = ConfigDict(
        json_schema_extra={
            "anyOf": [
                {"required": ["nature"]},
                {"required": ["currency"]},
            ]
        }
    )

    nature: Literal["asset", "liability"] = Field(default=None)  # type: ignore[arg-type]
    currency: CurrencyCode = Field(default=None)  # type: ignore[arg-type]

    @model_validator(mode="after")
    def validate_correction_fields(self) -> Self:
        fields = self.model_fields_set
        if not fields & {"nature", "currency"}:
            raise PydanticCustomError(
                "value_error",
                "Nature or Currency must be provided for semantic correction.",
            )
        if ("nature" in fields and self.nature is None) or (
            "currency" in fields and self.currency is None
        ):
            raise PydanticCustomError(
                "value_error",
                "Nature and Currency cannot be null for semantic correction.",
            )
        return self


type UpdateFinanceAccountRequest = Annotated[
    UpdateAccountRequest | CorrectAccountSemanticsRequest,
    Field(union_mode="left_to_right"),
]


class CategoryAllocationRequest(_RequestModel):
    """One complete v1 allocation, optionally Uncategorized."""

    amount: MoneyRequest
    category_id: UUID | None = Field(default=None, alias="categoryId")


class _TransactionNoteRequest(_RequestModel):
    """Shared optional plain-text note contract for Transaction commands."""

    note: str | None = Field(
        default=None,
        strict=True,
        json_schema_extra={"maxLength": 500},
    )

    @field_validator("note")
    @classmethod
    def normalize_note(cls, value: str | None) -> str | None:
        """Trim optional plain text, normalize blank to null, and enforce length."""

        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        if len(normalized) > 500:
            raise PydanticCustomError(
                "value_error",
                "Transaction note must not exceed 500 characters.",
            )
        return normalized


class _CreateOrdinaryTransactionRequest(_TransactionNoteRequest):
    """Common command fields for the first two Finance Transaction kinds."""

    account_id: UUID = Field(alias="accountId")
    transaction_date: FinanceRequestDate = Field(alias="transactionDate")
    economic_amount: MoneyRequest = Field(alias="economicAmount")
    category_allocations: list[CategoryAllocationRequest] = Field(
        alias="categoryAllocations",
        min_length=1,
        max_length=1,
    )

    @model_validator(mode="after")
    def validate_complete_allocation(self) -> Self:
        economic_amount = self.economic_amount.to_money()
        allocation_amount = self.category_allocations[0].amount.to_money()
        if economic_amount.amount <= 0 or allocation_amount.amount <= 0:
            raise PydanticCustomError(
                "value_error",
                "Economic Amount and Category Allocation amount must be positive.",
            )
        if allocation_amount != economic_amount:
            raise PydanticCustomError(
                "value_error",
                "The Category Allocation must equal the complete Economic Amount.",
            )
        return self


class CreateIncomeTransactionRequest(_CreateOrdinaryTransactionRequest):
    """Record value received from outside the Finance Ledger."""

    kind: Literal["income"]


class CreateExpenseTransactionRequest(_CreateOrdinaryTransactionRequest):
    """Record value spent outside the Finance Ledger."""

    kind: Literal["expense"]


class CreateInternalTransferTransactionRequest(_TransactionNoteRequest):
    """Record one atomic same-currency Transfer between two Accounts."""

    kind: Literal["internalTransfer"]
    source_account_id: UUID = Field(alias="sourceAccountId")
    destination_account_id: UUID = Field(alias="destinationAccountId")
    amount: MoneyRequest
    transaction_date: FinanceRequestDate = Field(alias="transactionDate")

    @model_validator(mode="after")
    def validate_transfer(self) -> Self:
        if self.source_account_id == self.destination_account_id:
            raise PydanticCustomError(
                "value_error",
                "Source and Destination Accounts must be distinct.",
            )
        if self.amount.to_money().amount <= 0:
            raise PydanticCustomError(
                "value_error",
                "Transfer amount must be positive.",
            )
        return self


class CreateBalanceAdjustmentRequest(_TransactionNoteRequest):
    """Stale-safe target-balance command for one active Account."""

    account_id: UUID = Field(alias="accountId")
    transaction_date: FinanceRequestDate = Field(alias="transactionDate")
    expected_derived_balance: MoneyRequest = Field(alias="expectedDerivedBalance")
    expected_account_nature: Literal["asset", "liability"] = Field(alias="expectedAccountNature")
    target_balance: MoneyRequest = Field(alias="targetBalance")


type CreateFinanceTransactionRequest = Annotated[
    CreateIncomeTransactionRequest
    | CreateExpenseTransactionRequest
    | CreateInternalTransferTransactionRequest,
    Field(discriminator="kind"),
]
