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


type FinanceTransactionResponse = Annotated[
    IncomeTransactionResponse | ExpenseTransactionResponse,
    Field(discriminator="kind"),
]


class _RequestModel(BaseModel):
    """Accept only declared public Finance JSON fields."""

    model_config = ConfigDict(
        extra="forbid",
        validate_by_alias=True,
        validate_by_name=False,
    )


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


class CategoryAllocationRequest(_RequestModel):
    """One complete v1 allocation, optionally Uncategorized."""

    amount: MoneyRequest
    category_id: UUID | None = Field(default=None, alias="categoryId")


class _CreateOrdinaryTransactionRequest(_RequestModel):
    """Common command fields for the first two Finance Transaction kinds."""

    account_id: UUID = Field(alias="accountId")
    transaction_date: FinanceRequestDate = Field(alias="transactionDate")
    economic_amount: MoneyRequest = Field(alias="economicAmount")
    category_allocations: list[CategoryAllocationRequest] = Field(
        alias="categoryAllocations",
        min_length=1,
        max_length=1,
    )
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


type CreateFinanceTransactionRequest = Annotated[
    CreateIncomeTransactionRequest | CreateExpenseTransactionRequest,
    Field(discriminator="kind"),
]
