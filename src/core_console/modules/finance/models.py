"""SQLAlchemy models for the durable Finance ledger."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
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
        UniqueConstraint(
            "id",
            "ledger_id",
            name="uq_finance_accounts_id_ledger_id",
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
        UniqueConstraint(
            "id",
            "ledger_id",
            name="uq_finance_categories_id_ledger_id",
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


class FinanceTransaction(Base):
    """One atomic classified event inside a Finance Ledger."""

    __tablename__ = "finance_transactions"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('income', 'expense', 'internal_transfer')",
            name="ck_finance_transactions_kind",
        ),
        CheckConstraint(
            "note IS NULL OR (note = btrim(note) AND char_length(note) BETWEEN 1 AND 500)",
            name="ck_finance_transactions_note",
        ),
        UniqueConstraint(
            "id",
            "ledger_id",
            name="uq_finance_transactions_id_ledger_id",
        ),
        Index(
            "ix_finance_transactions_ledger_date_id",
            "ledger_id",
            "transaction_date",
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
            name="fk_finance_transactions_ledger_id_finance_ledgers",
        ),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    transaction_date: Mapped[date] = mapped_column(Date, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
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


class FinanceAccountMovement(Base):
    """One signed account-relative balance change within a Transaction."""

    __tablename__ = "finance_account_movements"
    __table_args__ = (
        CheckConstraint(
            "amount NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name="ck_finance_account_movements_amount_finite",
        ),
        CheckConstraint(
            "amount <> 0",
            name="ck_finance_account_movements_amount_nonzero",
        ),
        CheckConstraint(
            "currency IN ('CNY', 'JPY', 'USD')",
            name="ck_finance_account_movements_currency",
        ),
        CheckConstraint(
            "(currency = 'JPY' AND scale(amount) = 0) OR "
            "(currency IN ('CNY', 'USD') AND scale(amount) <= 2)",
            name="ck_finance_account_movements_amount_scale",
        ),
        CheckConstraint(
            "role IN ('primary', 'source', 'destination')",
            name="ck_finance_account_movements_role",
        ),
        ForeignKeyConstraint(
            ["transaction_id", "ledger_id"],
            ["finance_transactions.id", "finance_transactions.ledger_id"],
            name="fk_finance_account_movements_transaction_ledger",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["account_id", "ledger_id"],
            ["finance_accounts.id", "finance_accounts.ledger_id"],
            name="fk_finance_account_movements_account_ledger",
        ),
        Index(
            "ix_finance_account_movements_account_transaction",
            "account_id",
            "transaction_id",
        ),
        UniqueConstraint(
            "transaction_id",
            "role",
            name="uq_finance_account_movements_transaction_role",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    transaction_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    ledger_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    account_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(), nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False, default="primary")


class FinanceCategoryAllocation(Base):
    """One complete positive Income or Expense classification amount."""

    __tablename__ = "finance_category_allocations"
    __table_args__ = (
        CheckConstraint(
            "amount NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name="ck_finance_category_allocations_amount_finite",
        ),
        CheckConstraint(
            "amount > 0",
            name="ck_finance_category_allocations_amount_positive",
        ),
        CheckConstraint(
            "currency IN ('CNY', 'JPY', 'USD')",
            name="ck_finance_category_allocations_currency",
        ),
        CheckConstraint(
            "(currency = 'JPY' AND scale(amount) = 0) OR "
            "(currency IN ('CNY', 'USD') AND scale(amount) <= 2)",
            name="ck_finance_category_allocations_amount_scale",
        ),
        ForeignKeyConstraint(
            ["transaction_id", "ledger_id"],
            ["finance_transactions.id", "finance_transactions.ledger_id"],
            name="fk_finance_category_allocations_transaction_ledger",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["category_id", "ledger_id"],
            ["finance_categories.id", "finance_categories.ledger_id"],
            name="fk_finance_category_allocations_category_ledger",
        ),
        UniqueConstraint(
            "transaction_id",
            name="uq_finance_category_allocations_transaction_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    transaction_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    ledger_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    category_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        nullable=True,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(), nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
