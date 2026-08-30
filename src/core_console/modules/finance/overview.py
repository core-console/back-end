"""Narrow read side for the calendar-first Finance Overview."""

from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import BigInteger, Date, Numeric, Text, and_, case, func, literal, select, union_all
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.modules.finance.models import (
    FinanceAccount,
    FinanceAccountMovement,
    FinanceCategoryAllocation,
    FinanceLedger,
    FinanceTransaction,
)
from core_console.modules.finance.money import CurrencyCode
from core_console.modules.finance.queries import (
    FinanceAccountBalance,
    current_finance_account_balance_expression,
)


@dataclass(frozen=True, slots=True)
class FinanceOverviewPosition:
    currency: CurrencyCode
    asset_total: Decimal
    liability_total: Decimal
    net_position: Decimal


@dataclass(frozen=True, slots=True)
class FinanceOverviewIncomeExpense:
    currency: CurrencyCode
    income: Decimal
    expense: Decimal
    net: Decimal


@dataclass(frozen=True, slots=True)
class FinanceOverviewTransactionCounts:
    income: int
    expense: int
    internal_transfer: int
    balance_adjustment: int


@dataclass(frozen=True, slots=True)
class FinanceOverviewDayCurrencyActivity(FinanceOverviewIncomeExpense):
    transaction_count: int


@dataclass(frozen=True, slots=True)
class FinanceOverviewDay:
    date: date
    transaction_count: int
    transaction_counts: FinanceOverviewTransactionCounts
    activity_by_currency: tuple[FinanceOverviewDayCurrencyActivity, ...]


@dataclass(frozen=True, slots=True)
class FinanceOverview:
    ledger: FinanceLedger
    month: str
    accounts: tuple[FinanceAccountBalance, ...]
    financial_positions: tuple[FinanceOverviewPosition, ...]
    month_summaries: tuple[FinanceOverviewIncomeExpense, ...]
    days: tuple[FinanceOverviewDay, ...]


def _typed_null(type_: Any) -> Any:
    return literal(None).cast(type_)


async def read_finance_overview(
    session: AsyncSession,
    *,
    ledger: FinanceLedger,
    month: str,
) -> FinanceOverview:
    """Read all overlapping Overview sections in one statement snapshot."""

    year, month_number = (int(part) for part in month.split("-"))
    month_start = date(year, month_number, 1)
    month_end = date(year, month_number, monthrange(year, month_number)[1])
    zero = Decimal(0)

    account_balances = (
        select(
            FinanceAccount.id,
            FinanceAccount.name,
            FinanceAccount.name_key,
            FinanceAccount.nature,
            FinanceAccount.currency,
            FinanceAccount.opening_balance,
            FinanceAccount.tracking_start_date,
            current_finance_account_balance_expression().label("current_balance"),
            FinanceAccount.status,
        )
        .where(FinanceAccount.ledger_id == ledger.id)
        .cte("overview_account_balances")
    )
    transaction_currencies = (
        select(
            FinanceTransaction.id.label("transaction_id"),
            FinanceTransaction.ledger_id,
            FinanceTransaction.transaction_date,
            FinanceTransaction.kind,
            FinanceAccountMovement.currency,
        )
        .join(
            FinanceAccountMovement,
            and_(
                FinanceAccountMovement.transaction_id == FinanceTransaction.id,
                FinanceAccountMovement.ledger_id == FinanceTransaction.ledger_id,
            ),
        )
        .where(
            FinanceTransaction.ledger_id == ledger.id,
            FinanceTransaction.transaction_date >= month_start,
            FinanceTransaction.transaction_date <= month_end,
        )
        .group_by(
            FinanceTransaction.id,
            FinanceTransaction.ledger_id,
            FinanceTransaction.transaction_date,
            FinanceTransaction.kind,
            FinanceAccountMovement.currency,
        )
        .cte("overview_transaction_currencies")
    )
    facts = (
        select(
            transaction_currencies,
            FinanceCategoryAllocation.amount.label("economic_amount"),
        )
        .outerjoin(
            FinanceCategoryAllocation,
            and_(
                FinanceCategoryAllocation.transaction_id == transaction_currencies.c.transaction_id,
                FinanceCategoryAllocation.ledger_id == transaction_currencies.c.ledger_id,
            ),
        )
        .cte("overview_transaction_facts")
    )

    income = func.coalesce(
        func.sum(case((facts.c.kind == "income", facts.c.economic_amount), else_=zero)),
        zero,
    )
    expense = func.coalesce(
        func.sum(case((facts.c.kind == "expense", facts.c.economic_amount), else_=zero)),
        zero,
    )
    currencies = select(account_balances.c.currency).distinct().cte("overview_currencies")
    summaries = (
        select(
            currencies.c.currency,
            income.label("income"),
            expense.label("expense"),
            (income - expense).label("net"),
        )
        .outerjoin(facts, facts.c.currency == currencies.c.currency)
        .group_by(currencies.c.currency)
        .cte("overview_month_summaries")
    )
    positions = (
        select(
            account_balances.c.currency,
            func.sum(
                case(
                    (account_balances.c.nature == "asset", account_balances.c.current_balance),
                    else_=zero,
                )
            ).label("asset_total"),
            func.sum(
                case(
                    (
                        account_balances.c.nature == "liability",
                        account_balances.c.current_balance,
                    ),
                    else_=zero,
                )
            ).label("liability_total"),
        )
        .group_by(account_balances.c.currency)
        .cte("overview_positions")
    )
    day_counts = (
        select(
            facts.c.transaction_date,
            func.count().label("transaction_count"),
            func.count().filter(facts.c.kind == "income").label("income_count"),
            func.count().filter(facts.c.kind == "expense").label("expense_count"),
            func.count().filter(facts.c.kind == "internal_transfer").label("transfer_count"),
            func.count().filter(facts.c.kind == "balance_adjustment").label("adjustment_count"),
        )
        .group_by(facts.c.transaction_date)
        .cte("overview_day_counts")
    )
    day_activity = (
        select(
            facts.c.transaction_date,
            facts.c.currency,
            income.label("income"),
            expense.label("expense"),
            (income - expense).label("net"),
            func.count().label("currency_transaction_count"),
        )
        .group_by(facts.c.transaction_date, facts.c.currency)
        .cte("overview_day_activity")
    )

    uuid_null = _typed_null(PostgreSQLUUID(as_uuid=True))
    text_null = _typed_null(Text())
    numeric_null = _typed_null(Numeric())
    date_null = _typed_null(Date())
    count_null = _typed_null(BigInteger())
    account_rows = select(
        literal("account").label("row_type"),
        account_balances.c.id,
        account_balances.c.name,
        account_balances.c.name_key,
        account_balances.c.nature,
        account_balances.c.currency,
        account_balances.c.opening_balance,
        account_balances.c.tracking_start_date,
        account_balances.c.current_balance,
        account_balances.c.status,
        numeric_null.label("amount_1"),
        numeric_null.label("amount_2"),
        numeric_null.label("amount_3"),
        date_null.label("activity_date"),
        count_null.label("transaction_count"),
        count_null.label("income_count"),
        count_null.label("expense_count"),
        count_null.label("transfer_count"),
        count_null.label("adjustment_count"),
        count_null.label("currency_transaction_count"),
    )
    position_rows = select(
        literal("position"),
        uuid_null,
        text_null,
        text_null,
        text_null,
        positions.c.currency,
        numeric_null,
        date_null,
        numeric_null,
        text_null,
        positions.c.asset_total,
        positions.c.liability_total,
        positions.c.asset_total - positions.c.liability_total,
        date_null,
        count_null,
        count_null,
        count_null,
        count_null,
        count_null,
        count_null,
    )
    summary_rows = select(
        literal("summary"),
        uuid_null,
        text_null,
        text_null,
        text_null,
        summaries.c.currency,
        numeric_null,
        date_null,
        numeric_null,
        text_null,
        summaries.c.income,
        summaries.c.expense,
        summaries.c.net,
        date_null,
        count_null,
        count_null,
        count_null,
        count_null,
        count_null,
        count_null,
    )
    day_rows = select(
        literal("day"),
        uuid_null,
        text_null,
        text_null,
        text_null,
        day_activity.c.currency,
        numeric_null,
        date_null,
        numeric_null,
        text_null,
        day_activity.c.income,
        day_activity.c.expense,
        day_activity.c.net,
        day_activity.c.transaction_date,
        day_counts.c.transaction_count,
        day_counts.c.income_count,
        day_counts.c.expense_count,
        day_counts.c.transfer_count,
        day_counts.c.adjustment_count,
        day_activity.c.currency_transaction_count,
    ).join(day_counts, day_counts.c.transaction_date == day_activity.c.transaction_date)
    result = await session.execute(union_all(account_rows, position_rows, summary_rows, day_rows))
    return _assemble_overview(ledger=ledger, month=month, rows=list(result.mappings()))


def _assemble_overview(*, ledger: FinanceLedger, month: str, rows: list[Any]) -> FinanceOverview:
    accounts: list[tuple[str, str, FinanceAccountBalance]] = []
    positions: list[FinanceOverviewPosition] = []
    summaries: list[FinanceOverviewIncomeExpense] = []
    days: dict[
        date, tuple[FinanceOverviewTransactionCounts, int, list[FinanceOverviewDayCurrencyActivity]]
    ] = {}
    for row in rows:
        currency = cast(CurrencyCode, row["currency"])
        if row["row_type"] == "account":
            account = FinanceAccount(
                id=row["id"],
                ledger_id=ledger.id,
                name=row["name"],
                name_key=row["name_key"],
                nature=row["nature"],
                currency=currency,
                opening_balance=row["opening_balance"],
                tracking_start_date=row["tracking_start_date"],
                status=row["status"],
            )
            accounts.append(
                (
                    row["status"],
                    row["name_key"],
                    FinanceAccountBalance(account, row["current_balance"]),
                )
            )
        elif row["row_type"] == "position":
            positions.append(
                FinanceOverviewPosition(currency, row["amount_1"], row["amount_2"], row["amount_3"])
            )
        elif row["row_type"] == "summary":
            summaries.append(
                FinanceOverviewIncomeExpense(
                    currency, row["amount_1"], row["amount_2"], row["amount_3"]
                )
            )
        else:
            activity_date = cast(date, row["activity_date"])
            counts = FinanceOverviewTransactionCounts(
                row["income_count"],
                row["expense_count"],
                row["transfer_count"],
                row["adjustment_count"],
            )
            day = days.setdefault(activity_date, (counts, row["transaction_count"], []))
            day[2].append(
                FinanceOverviewDayCurrencyActivity(
                    currency,
                    row["amount_1"],
                    row["amount_2"],
                    row["amount_3"],
                    row["currency_transaction_count"],
                )
            )
    accounts.sort(key=lambda item: (0 if item[0] == "active" else 1, item[1], item[2].account.id))
    day_models = tuple(
        FinanceOverviewDay(
            day, total, counts, tuple(sorted(activity, key=lambda item: item.currency))
        )
        for day, (counts, total, activity) in sorted(days.items())
    )
    return FinanceOverview(
        ledger,
        month,
        tuple(item[2] for item in accounts),
        tuple(sorted(positions, key=lambda item: item.currency)),
        tuple(sorted(summaries, key=lambda item: item.currency)),
        day_models,
    )
