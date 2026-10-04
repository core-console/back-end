"""Frozen v1 create validity; independent of evolving resource request schemas."""

from datetime import date
from decimal import Decimal
from re import fullmatch
from typing import Annotated, Literal, Self

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


type CreateOperation = Literal[
    "createFinanceLedger", "createFinanceAccount", "createFinanceCategory"
]
