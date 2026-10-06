"""Frozen v1 create validity; independent of evolving resource request schemas."""

from datetime import date
from decimal import Decimal
from re import fullmatch
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

from core_console.modules.finance.money import Money


class LedgerCommandV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(strict=True, max_length=100)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("Ledger name must not be blank.")
        return name


class CategoryCommandV1(LedgerCommandV1):
    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("Category name must not be blank.")
        return name


class OpeningBalanceV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: str = Field(
        strict=True,
        description=(
            "Plain base-10 decimal string. A non-zero value may have at most 131,072 "
            "integer digits after insignificant leading zeros are removed. V1 precision "
            "is CNY/USD: 2 and JPY: 0; excess fractional digits are invalid, even zeros."
        ),
    )
    currency: Literal["CNY", "JPY", "USD"]

    @model_validator(mode="after")
    def validate_money(self) -> Self:
        # These rules and limits belong to v1. Changing the currency catalog or
        # resource validators must not relax a command previously proven invalid.
        if fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", self.amount) is None:
            raise ValueError("Money amount must be a plain base-10 decimal string.")
        scale = 0 if self.currency == "JPY" else 2
        if len(self.amount.partition(".")[2]) > scale:
            raise ValueError("Money amount exceeds the currency's supported precision.")
        value = Decimal(self.amount)
        if not value.is_zero() and value.adjusted() + 1 > 131_072:
            raise ValueError(
                "Money amount exceeds the PostgreSQL durable range of 131072 integer digits."
            )
        return self

    def to_money(self) -> Money:
        return Money(amount=Decimal(self.amount), currency=self.currency)

    def canonical(self) -> dict[str, str]:
        value = Decimal(self.amount)
        if value == 0:
            value = Decimal(0)
        scale = 0 if self.currency == "JPY" else 2
        return {"amount": f"{value:.{scale}f}", "currency": self.currency}


def _civil_date_v1(value: object) -> object:
    if not isinstance(value, str) or fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
        raise ValueError("Date must use exact ISO YYYY-MM-DD format.")
    return value


class AccountCommandV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(strict=True, max_length=100)
    nature: Literal["asset", "liability"]
    currency: Literal["CNY", "JPY", "USD"]
    opening_balance: OpeningBalanceV1 = Field(alias="openingBalance")
    tracking_start_date: Annotated[date, BeforeValidator(_civil_date_v1)] = Field(
        alias="trackingStartDate"
    )

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("Account name must not be blank.")
        return name

    @model_validator(mode="after")
    def matching_currency(self) -> Self:
        if self.opening_balance.currency != self.currency:
            raise ValueError("Opening Balance currency must match the Account currency.")
        return self

    def canonical(self) -> dict[str, object]:
        return {
            "name": self.name,
            "nature": self.nature,
            "currency": self.currency,
            "openingBalance": self.opening_balance.canonical(),
            "trackingStartDate": self.tracking_start_date.isoformat(),
        }


class _TransactionCommandV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transaction_date: Annotated[date, BeforeValidator(_civil_date_v1)] = Field(
        alias="transactionDate"
    )
    note: str | None = Field(default=None, strict=True, json_schema_extra={"maxLength": 500})

    @field_validator("note")
    @classmethod
    def normalize_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if len(value) > 500:
            raise ValueError("Transaction note must not exceed 500 characters.")
        return value or None


class AllocationV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: OpeningBalanceV1
    category_id: UUID | None = Field(default=None, alias="categoryId")


class OrdinaryTransactionCommandV1(_TransactionCommandV1):
    kind: Literal["income", "expense"]
    account_id: UUID = Field(alias="accountId")
    economic_amount: OpeningBalanceV1 = Field(alias="economicAmount")
    category_allocations: list[AllocationV1] = Field(
        alias="categoryAllocations", min_length=1, max_length=1
    )

    @model_validator(mode="after")
    def complete_allocation(self) -> Self:
        allocation = self.category_allocations[0].amount
        economic = self.economic_amount
        if Decimal(economic.amount) <= 0 or Decimal(allocation.amount) <= 0:
            raise ValueError("Economic Amount and Category Allocation amount must be positive.")
        if allocation.currency != economic.currency or Decimal(allocation.amount) != Decimal(
            economic.amount
        ):
            raise ValueError("The Category Allocation must equal the complete Economic Amount.")
        return self

    def canonical(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "accountId": str(self.account_id),
            "transactionDate": self.transaction_date.isoformat(),
            "economicAmount": self.economic_amount.canonical(),
            "categoryAllocations": [
                {
                    "amount": allocation.amount.canonical(),
                    "categoryId": (
                        str(allocation.category_id) if allocation.category_id is not None else None
                    ),
                }
                for allocation in self.category_allocations
            ],
            "note": self.note,
        }


class IncomeCommandV1(OrdinaryTransactionCommandV1):
    kind: Literal["income"]


class ExpenseCommandV1(OrdinaryTransactionCommandV1):
    kind: Literal["expense"]


class TransferCommandV1(_TransactionCommandV1):
    kind: Literal["internalTransfer"]
    source_account_id: UUID = Field(alias="sourceAccountId")
    destination_account_id: UUID = Field(alias="destinationAccountId")
    amount: OpeningBalanceV1

    @model_validator(mode="after")
    def valid_transfer(self) -> Self:
        if self.source_account_id == self.destination_account_id:
            raise ValueError("Source and Destination Accounts must be distinct.")
        if Decimal(self.amount.amount) <= 0:
            raise ValueError("Transfer amount must be positive.")
        return self

    def canonical(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "sourceAccountId": str(self.source_account_id),
            "destinationAccountId": str(self.destination_account_id),
            "amount": self.amount.canonical(),
            "transactionDate": self.transaction_date.isoformat(),
            "note": self.note,
        }


type TransactionCommandV1 = Annotated[
    IncomeCommandV1 | ExpenseCommandV1 | TransferCommandV1, Field(discriminator="kind")
]


class AdjustmentCommandV1(_TransactionCommandV1):
    account_id: UUID = Field(alias="accountId")
    expected_derived_balance: OpeningBalanceV1 = Field(alias="expectedDerivedBalance")
    expected_account_nature: Literal["asset", "liability"] = Field(alias="expectedAccountNature")
    target_balance: OpeningBalanceV1 = Field(alias="targetBalance")

    def canonical(self) -> dict[str, object]:
        return {
            "accountId": str(self.account_id),
            "transactionDate": self.transaction_date.isoformat(),
            "expectedDerivedBalance": self.expected_derived_balance.canonical(),
            "expectedAccountNature": self.expected_account_nature,
            "targetBalance": self.target_balance.canonical(),
            "note": self.note,
        }


type CreateOperation = Literal[
    "createFinanceLedger",
    "createFinanceAccount",
    "createFinanceCategory",
    "createFinanceTransaction",
    "createBalanceAdjustment",
]
