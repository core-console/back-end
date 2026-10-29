"""Immutable origin fragments and linked transfers for actual credit returns."""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Integer, Numeric, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from core_console.database.base import Base


class FinanceCreditReturnFragment(Base):
    __tablename__ = "finance_credit_return_fragments"
    __table_args__ = (
        UniqueConstraint("transaction_id", name="uq_finance_credit_return_transaction"),
        UniqueConstraint(
            "settlement_id", "origin_settlement_id", name="uq_finance_credit_return_origin"
        ),
        ForeignKeyConstraint(
            ["settlement_id", "ledger_id"],
            ["finance_settlements.id", "finance_settlements.ledger_id"],
        ),
        ForeignKeyConstraint(
            ["origin_settlement_id", "ledger_id"],
            ["finance_credit_origins.settlement_id", "finance_credit_origins.ledger_id"],
        ),
        ForeignKeyConstraint(
            ["transaction_id", "ledger_id"],
            ["finance_transactions.id", "finance_transactions.ledger_id"],
        ),
        CheckConstraint("position >= 1", name="ck_finance_credit_return_position"),
        CheckConstraint(
            "amount > 0 AND amount NOT IN "
            "('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name="ck_finance_credit_return_amount",
        ),
        CheckConstraint(
            "origin_available_before >= amount AND origin_available_before NOT IN "
            "('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name="ck_finance_credit_return_available",
        ),
    )
    settlement_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    ledger_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True))
    origin_settlement_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True))
    transaction_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True))
    amount: Mapped[Decimal] = mapped_column(Numeric())
    origin_available_before: Mapped[Decimal] = mapped_column(Numeric())
