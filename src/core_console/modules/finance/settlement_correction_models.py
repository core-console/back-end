"""Retained cash receipt corrections and their protected financial restatements."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core_console.database.base import Base


class FinanceSettlementCorrection(Base):
    __tablename__ = "finance_settlement_corrections"
    __table_args__ = (
        ForeignKeyConstraint(
            ["settlement_id", "ledger_id"],
            ["finance_settlements.id", "finance_settlements.ledger_id"],
        ),
        ForeignKeyConstraint(
            ["ledger_id", "actor_id"], ["finance_ledgers.id", "finance_ledgers.owner_id"]
        ),
        UniqueConstraint("id", "ledger_id", name="uq_finance_settlement_corrections_ledger"),
        UniqueConstraint(
            "settlement_id", "revision", name="uq_finance_settlement_corrections_revision"
        ),
        CheckConstraint("revision > 0", name="ck_finance_settlement_corrections_revision"),
        CheckConstraint(
            "jsonb_typeof(plan) = 'object'", name="ck_finance_settlement_corrections_plan"
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    ledger_id: Mapped[UUID]
    settlement_id: Mapped[UUID]
    actor_id: Mapped[UUID]
    revision: Mapped[int] = mapped_column(Integer)
    plan: Mapped[dict[str, object]] = mapped_column(JSONB)
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )


class FinanceSettlementCorrectionTransaction(Base):
    __tablename__ = "finance_settlement_correction_transactions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["correction_id", "ledger_id"],
            ["finance_settlement_corrections.id", "finance_settlement_corrections.ledger_id"],
        ),
        ForeignKeyConstraint(
            ["transaction_id", "ledger_id"],
            ["finance_transactions.id", "finance_transactions.ledger_id"],
        ),
    )
    transaction_id: Mapped[UUID] = mapped_column(primary_key=True)
    ledger_id: Mapped[UUID]
    correction_id: Mapped[UUID]
