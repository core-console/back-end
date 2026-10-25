"""Confirmed cash receipts and their immutable, ordered principal Allocations."""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from core_console.database.base import Base


class FinanceSettlement(Base):
    __tablename__ = "finance_settlements"
    __table_args__ = (
        CheckConstraint(
            "(receiving_account_id IS NOT NULL AND reason IS NULL) OR "
            "(receiving_account_id IS NULL AND reason IS NOT NULL "
            "AND reason = btrim(reason) AND char_length(reason) BETWEEN 1 AND 500 "
            "AND selected_obligation_ids IS NOT NULL)",
            name="ck_finance_settlements_action",
        ),
        CheckConstraint(
            "selected_obligation_ids IS NULL OR (cardinality(selected_obligation_ids) > 0 "
            "AND array_position(selected_obligation_ids, NULL) IS NULL)",
            name="ck_finance_settlements_selection",
        ),
        UniqueConstraint("id", "ledger_id", name="uq_finance_settlements_id_ledger"),
        UniqueConstraint("confirmation_order", name="uq_finance_settlements_order"),
        ForeignKeyConstraint(["ledger_id"], ["finance_ledgers.id"]),
        ForeignKeyConstraint(
            ["participant_id", "ledger_id"],
            ["finance_participants.id", "finance_participants.ledger_id"],
        ),
        ForeignKeyConstraint(
            ["receiving_account_id", "ledger_id"],
            ["finance_accounts.id", "finance_accounts.ledger_id"],
        ),
        CheckConstraint(
            "amount > 0 AND amount NOT IN "
            "('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name="ck_finance_settlements_amount",
        ),
        CheckConstraint(
            "currency IN ('CNY', 'USD', 'JPY')", name="ck_finance_settlements_currency"
        ),
        CheckConstraint(
            "(currency = 'JPY' AND scale(amount) = 0) OR "
            "(currency IN ('CNY', 'USD') AND scale(amount) <= 2)",
            name="ck_finance_settlements_scale",
        ),
        CheckConstraint(
            "note IS NULL OR (note = btrim(note) AND char_length(note) BETWEEN 1 AND 500)",
            name="ck_finance_settlements_note",
        ),
    )
    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    ledger_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True))
    participant_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True))
    receiving_account_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    reason: Mapped[str | None] = mapped_column(Text)
    transaction_date: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Numeric())
    currency: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    selected_obligation_ids: Mapped[list[UUID] | None] = mapped_column(
        ARRAY(PostgreSQLUUID(as_uuid=True))
    )
    confirmation_order: Mapped[int] = mapped_column(BigInteger, Identity())
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("clock_timestamp()")
    )


class FinanceSettlementAllocation(Base):
    __tablename__ = "finance_settlement_allocations"
    __table_args__ = (
        UniqueConstraint("settlement_id", "obligation_id", name="uq_finance_settlement_obligation"),
        UniqueConstraint("transaction_id", name="uq_finance_settlement_transaction"),
        ForeignKeyConstraint(
            ["settlement_id", "ledger_id"],
            ["finance_settlements.id", "finance_settlements.ledger_id"],
        ),
        ForeignKeyConstraint(
            ["obligation_id", "ledger_id"],
            ["finance_obligations.id", "finance_obligations.ledger_id"],
        ),
        ForeignKeyConstraint(
            ["transaction_id", "ledger_id"],
            ["finance_transactions.id", "finance_transactions.ledger_id"],
        ),
        CheckConstraint("position >= 1", name="ck_finance_settlement_allocation_position"),
        CheckConstraint(
            "cash_paid >= 0 AND cash_paid NOT IN "
            "('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name="ck_finance_settlement_allocation_cash",
        ),
        CheckConstraint(
            "(kind = 'cash' AND cash_paid > 0 AND forgiven = 0) OR "
            "(kind = 'forgiveness' AND cash_paid = 0 AND forgiven > 0 "
            "AND forgiven NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric))",
            name="ck_finance_settlement_allocation_kind",
        ),
        CheckConstraint(
            "remaining_after >= 0 AND remaining_after NOT IN "
            "('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name="ck_finance_settlement_allocation_remaining",
        ),
    )
    settlement_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    ledger_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True))
    obligation_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True))
    transaction_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True))
    cash_paid: Mapped[Decimal] = mapped_column(Numeric())
    kind: Mapped[str] = mapped_column(Text, server_default="cash")
    forgiven: Mapped[Decimal] = mapped_column(Numeric(), server_default="0")
    remaining_after: Mapped[Decimal] = mapped_column(Numeric())


class FinanceCreditOrigin(Base):
    """One actual receipt's unallocated cash and its protected Liability effect."""

    __tablename__ = "finance_credit_origins"
    __table_args__ = (
        UniqueConstraint("liability_account_id", name="uq_finance_credit_account"),
        UniqueConstraint("transaction_id", name="uq_finance_credit_transaction"),
        ForeignKeyConstraint(
            ["settlement_id", "ledger_id"],
            ["finance_settlements.id", "finance_settlements.ledger_id"],
        ),
        ForeignKeyConstraint(
            ["liability_account_id", "ledger_id"],
            ["finance_accounts.id", "finance_accounts.ledger_id"],
        ),
        ForeignKeyConstraint(
            ["transaction_id", "ledger_id"],
            ["finance_transactions.id", "finance_transactions.ledger_id"],
        ),
        CheckConstraint(
            "amount > 0 AND amount NOT IN "
            "('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name="ck_finance_credit_amount",
        ),
    )
    settlement_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    ledger_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True))
    liability_account_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True))
    transaction_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True))
    amount: Mapped[Decimal] = mapped_column(Numeric())


class FinanceSettlementIncome(Base):
    """Explicitly accepted receipt surplus linked to its protected Income."""

    __tablename__ = "finance_settlement_income"
    __table_args__ = (
        UniqueConstraint("transaction_id", name="uq_finance_settlement_income_transaction"),
        ForeignKeyConstraint(
            ["settlement_id", "ledger_id"],
            ["finance_settlements.id", "finance_settlements.ledger_id"],
        ),
        ForeignKeyConstraint(
            ["transaction_id", "ledger_id"],
            ["finance_transactions.id", "finance_transactions.ledger_id"],
        ),
        CheckConstraint(
            "amount > 0 AND amount NOT IN "
            "('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name="ck_finance_settlement_income_amount",
        ),
    )
    settlement_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    ledger_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True))
    transaction_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True))
    amount: Mapped[Decimal] = mapped_column(Numeric())
