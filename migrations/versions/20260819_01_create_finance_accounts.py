"""Create account-relative Finance Accounts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260819_01"
down_revision = "20260818_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create Finance Accounts without a stored Current Balance authority."""

    op.create_table(
        "finance_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ledger_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("name_key", sa.Text(collation="C"), nullable=False),
        sa.Column("nature", sa.Text(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("opening_balance", sa.Numeric(), nullable=False),
        sa.Column("tracking_start_date", sa.Date(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "regexp_replace(name, '^[[:space:]]+|[[:space:]]+$', '', 'g') <> ''",
            name="ck_finance_accounts_name_not_blank",
        ),
        sa.CheckConstraint(
            "name = regexp_replace(name, '^[[:space:]]+|[[:space:]]+$', '', 'g')",
            name="ck_finance_accounts_name_trimmed",
        ),
        sa.CheckConstraint(
            "char_length(name) <= 100",
            name="ck_finance_accounts_name_length",
        ),
        sa.CheckConstraint(
            "name_key <> ''",
            name="ck_finance_accounts_name_key_not_blank",
        ),
        sa.CheckConstraint(
            "nature IN ('asset', 'liability')",
            name="ck_finance_accounts_nature",
        ),
        sa.CheckConstraint(
            "currency IN ('CNY', 'JPY', 'USD')",
            name="ck_finance_accounts_currency",
        ),
        sa.CheckConstraint(
            "opening_balance NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name="ck_finance_accounts_opening_balance_finite",
        ),
        sa.CheckConstraint(
            "(currency = 'JPY' AND scale(opening_balance) = 0) OR "
            "(currency IN ('CNY', 'USD') AND scale(opening_balance) <= 2)",
            name="ck_finance_accounts_opening_balance_scale",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'archived')",
            name="ck_finance_accounts_status",
        ),
        sa.ForeignKeyConstraint(
            ["ledger_id"],
            ["finance_ledgers.id"],
            name="fk_finance_accounts_ledger_id_finance_ledgers",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_finance_accounts"),
    )
    op.create_index(
        "ix_finance_accounts_ledger_status_name_key_id",
        "finance_accounts",
        ["ledger_id", "status", "name_key", "id"],
    )


def downgrade() -> None:
    """Drop Finance Accounts."""

    op.drop_index(
        "ix_finance_accounts_ledger_status_name_key_id",
        table_name="finance_accounts",
    )
    op.drop_table("finance_accounts")
