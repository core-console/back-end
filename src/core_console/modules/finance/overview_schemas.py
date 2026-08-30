"""Closed public schemas for the Finance Overview read model."""

from datetime import date
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints
from pydantic_core import PydanticCustomError

from core_console.modules.finance.money import CurrencyCode
from core_console.modules.finance.schemas import AccountResponse, LedgerResponse, MoneyResponse


def _validate_overview_month(value: str) -> str:
    try:
        date.fromisoformat(f"{value}-01")
    except ValueError:
        raise PydanticCustomError(
            "month_format",
            "Month must use exact valid ISO YYYY-MM format.",
        ) from None
    return value


type FinanceOverviewMonth = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"),
    AfterValidator(_validate_overview_month),
]


class FinancialPositionByCurrencyResponse(BaseModel):
    """Present account-relative position for one Account Currency."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    currency: CurrencyCode
    asset_total: MoneyResponse = Field(alias="assetTotal")
    liability_total: MoneyResponse = Field(alias="liabilityTotal")
    net_position: MoneyResponse = Field(alias="netPosition")


class IncomeExpenseByCurrencyResponse(BaseModel):
    """Income, Expense, and net activity for one Currency."""

    model_config = ConfigDict(extra="forbid")

    currency: CurrencyCode
    income: MoneyResponse
    expense: MoneyResponse
    net: MoneyResponse


class TransactionCountByKindResponse(BaseModel):
    """Finance Transaction counts for all four durable kinds."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    income: int = Field(ge=0)
    expense: int = Field(ge=0)
    internal_transfer: int = Field(alias="internalTransfer", ge=0)
    balance_adjustment: int = Field(alias="balanceAdjustment", ge=0)


class DayActivityByCurrencyResponse(IncomeExpenseByCurrencyResponse):
    """One Currency's aggregate activity on a calendar date."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    transaction_count: int = Field(alias="transactionCount", ge=0)


class FinanceOverviewDayResponse(BaseModel):
    """One sparse calendar date containing Finance activity."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    date: date
    transaction_count: int = Field(alias="transactionCount", ge=0)
    transaction_count_by_kind: TransactionCountByKindResponse = Field(
        alias="transactionCountByKind"
    )
    activity_by_currency: list[DayActivityByCurrencyResponse] = Field(alias="activityByCurrency")


class FinanceOverviewResponse(BaseModel):
    """Calendar-first present-position and selected-month Finance projection."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    ledger: LedgerResponse
    month: FinanceOverviewMonth
    accounts: list[AccountResponse]
    financial_position_by_currency: list[FinancialPositionByCurrencyResponse] = Field(
        alias="financialPositionByCurrency"
    )
    month_summary_by_currency: list[IncomeExpenseByCurrencyResponse] = Field(
        alias="monthSummaryByCurrency"
    )
    days: list[FinanceOverviewDayResponse]
