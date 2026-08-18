"""SQLAlchemy models for persisted Finance Ledgers."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from core_console.database.base import Base


def _utc_now() -> datetime:
    """Return an aware UTC timestamp for ORM-managed changes."""

    return datetime.now(UTC)


class FinanceLedger(Base):
    """Persisted personal Finance ownership boundary."""

    __tablename__ = "finance_ledgers"
    __table_args__ = (
        CheckConstraint(
            "regexp_replace(name, '^[[:space:]]+|[[:space:]]+$', '', 'g') <> ''",
            name="ck_finance_ledgers_name_not_blank",
        ),
        CheckConstraint(
            "name = regexp_replace(name, '^[[:space:]]+|[[:space:]]+$', '', 'g')",
            name="ck_finance_ledgers_name_trimmed",
        ),
        CheckConstraint(
            "char_length(name) <= 100",
            name="ck_finance_ledgers_name_length",
        ),
        CheckConstraint(
            "name_key <> ''",
            name="ck_finance_ledgers_name_key_not_blank",
        ),
        UniqueConstraint(
            "owner_id",
            "name_key",
            name="uq_finance_ledgers_owner_name_key",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    owner_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("users.id", name="fk_finance_ledgers_owner_id_users"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    name_key: Mapped[str] = mapped_column(Text(collation="C"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        onupdate=_utc_now,
        server_default=text("CURRENT_TIMESTAMP"),
    )
