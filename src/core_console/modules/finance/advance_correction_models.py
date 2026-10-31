"""Retained before/after evidence for coordinated original-fact corrections."""

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


class FinanceAdvanceCorrection(Base):
    __tablename__ = "finance_advance_corrections"
    __table_args__ = (
        ForeignKeyConstraint(
            ["advance_id", "ledger_id"], ["finance_advances.id", "finance_advances.ledger_id"]
        ),
        ForeignKeyConstraint(
            ["ledger_id", "actor_id"], ["finance_ledgers.id", "finance_ledgers.owner_id"]
        ),
        UniqueConstraint("advance_id", "revision", name="uq_finance_advance_corrections_revision"),
        CheckConstraint("revision > 0", name="ck_finance_advance_corrections_revision"),
        CheckConstraint(
            "jsonb_typeof(plan) = 'object'", name="ck_finance_advance_corrections_plan"
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    ledger_id: Mapped[UUID]
    advance_id: Mapped[UUID]
    actor_id: Mapped[UUID]
    revision: Mapped[int] = mapped_column(Integer)
    plan: Mapped[dict[str, object]] = mapped_column(JSONB)
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
