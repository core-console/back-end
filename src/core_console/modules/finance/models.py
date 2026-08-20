"""SQLAlchemy models for persisted Finance Ledgers, Accounts, and Categories."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
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


class FinanceAccount(Base):
    """Persisted account-relative financial position within one Ledger."""

    __tablename__ = "finance_accounts"
    __table_args__ = (
        CheckConstraint(
            "regexp_replace(name, '^[[:space:]]+|[[:space:]]+$', '', 'g') <> ''",
            name="ck_finance_accounts_name_not_blank",
        ),
        CheckConstraint(
            "name = regexp_replace(name, '^[[:space:]]+|[[:space:]]+$', '', 'g')",
            name="ck_finance_accounts_name_trimmed",
        ),
        CheckConstraint(
            "char_length(name) <= 100",
            name="ck_finance_accounts_name_length",
        ),
        CheckConstraint(
            "name_key <> ''",
            name="ck_finance_accounts_name_key_not_blank",
        ),
        CheckConstraint(
            "nature IN ('asset', 'liability')",
            name="ck_finance_accounts_nature",
        ),
        CheckConstraint(
            "currency IN ('CNY', 'JPY', 'USD')",
            name="ck_finance_accounts_currency",
        ),
        CheckConstraint(
            "opening_balance NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name="ck_finance_accounts_opening_balance_finite",
        ),
        CheckConstraint(
            "(currency = 'JPY' AND scale(opening_balance) = 0) OR "
            "(currency IN ('CNY', 'USD') AND scale(opening_balance) <= 2)",
            name="ck_finance_accounts_opening_balance_scale",
        ),
        CheckConstraint(
            "status IN ('active', 'archived')",
            name="ck_finance_accounts_status",
        ),
        Index(
            "ix_finance_accounts_ledger_status_name_key_id",
            "ledger_id",
            "status",
            "name_key",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    ledger_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey(
            "finance_ledgers.id",
            name="fk_finance_accounts_ledger_id_finance_ledgers",
        ),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    name_key: Mapped[str] = mapped_column(Text(collation="C"), nullable=False)
    nature: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    opening_balance: Mapped[Decimal] = mapped_column(Numeric(), nullable=False)
    tracking_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
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


class FinanceCategory(Base):
    """Persisted neutral classification owned by one Finance Ledger."""

    __tablename__ = "finance_categories"
    __table_args__ = (
        CheckConstraint(
            "regexp_replace(name, '^[[:space:]]+|[[:space:]]+$', '', 'g') <> ''",
            name="ck_finance_categories_name_not_blank",
        ),
        CheckConstraint(
            "name = regexp_replace(name, '^[[:space:]]+|[[:space:]]+$', '', 'g')",
            name="ck_finance_categories_name_trimmed",
        ),
        CheckConstraint(
            "char_length(name) <= 100",
            name="ck_finance_categories_name_length",
        ),
        CheckConstraint(
            "name_key <> ''",
            name="ck_finance_categories_name_key_not_blank",
        ),
        CheckConstraint(
            "status IN ('active', 'archived')",
            name="ck_finance_categories_status",
        ),
        UniqueConstraint(
            "ledger_id",
            "name_key",
            name="uq_finance_categories_ledger_name_key",
        ),
        Index(
            "ix_finance_categories_ledger_status_name_key_id",
            "ledger_id",
            "status",
            "name_key",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    ledger_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey(
            "finance_ledgers.id",
            name="fk_finance_categories_ledger_id_finance_ledgers",
        ),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    name_key: Mapped[str] = mapped_column(Text(collation="C"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
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
