"""Confirmed pure Advances and their single attributed Obligation."""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from core_console.database.base import Base


class FinanceAdvance(Base):
    """Immutable source of one Asset-funded, zero-owner-share event."""

    __tablename__ = "finance_advances"
    __table_args__ = (
        UniqueConstraint("id", "ledger_id", name="uq_finance_advances_id_ledger"),
        UniqueConstraint("transaction_id", name="uq_finance_advances_transaction"),
        ForeignKeyConstraint(["ledger_id"], ["finance_ledgers.id"]),
        ForeignKeyConstraint(
            ["funding_account_id", "ledger_id"],
            ["finance_accounts.id", "finance_accounts.ledger_id"],
        ),
        ForeignKeyConstraint(
            ["transaction_id", "ledger_id"],
            ["finance_transactions.id", "finance_transactions.ledger_id"],
        ),
        CheckConstraint(
            "note IS NULL OR (note = btrim(note) AND char_length(note) BETWEEN 1 AND 500)",
            name="ck_finance_advances_note",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    ledger_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True))
    funding_account_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True))
    transaction_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True))
    transaction_date: Mapped[date] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(Text)
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )


class FinanceObligation(Base):
    """Principal with an exclusive receivable position; resolution is added later."""

    __tablename__ = "finance_obligations"
    __table_args__ = (
        UniqueConstraint("advance_id", name="uq_finance_obligations_advance"),
        UniqueConstraint("receivable_account_id", name="uq_finance_obligations_receivable"),
        ForeignKeyConstraint(
            ["advance_id", "ledger_id"], ["finance_advances.id", "finance_advances.ledger_id"]
        ),
        ForeignKeyConstraint(
            ["participant_id", "ledger_id"],
            ["finance_participants.id", "finance_participants.ledger_id"],
        ),
        ForeignKeyConstraint(
            ["receivable_account_id", "ledger_id"],
            ["finance_accounts.id", "finance_accounts.ledger_id"],
        ),
        CheckConstraint(
            "principal > 0 AND principal NOT IN "
            "('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name="ck_finance_obligations_principal",
        ),
        CheckConstraint(
            "currency IN ('CNY', 'USD', 'JPY')", name="ck_finance_obligations_currency"
        ),
        CheckConstraint(
            "(currency = 'JPY' AND scale(principal) = 0) OR "
            "(currency IN ('CNY', 'USD') AND scale(principal) <= 2)",
            name="ck_finance_obligations_scale",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    ledger_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True))
    advance_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True))
    participant_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True))
    receivable_account_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True))
    principal: Mapped[Decimal] = mapped_column(Numeric())
    currency: Mapped[str] = mapped_column(Text)
