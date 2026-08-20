"""Create Ledger-owned Finance Categories."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260820_01"
down_revision = "20260819_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create durable neutral Categories with all-status name uniqueness."""

    op.create_table(
        "finance_categories",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ledger_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("name_key", sa.Text(collation="C"), nullable=False),
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
            name="ck_finance_categories_name_not_blank",
        ),
        sa.CheckConstraint(
            "name = regexp_replace(name, '^[[:space:]]+|[[:space:]]+$', '', 'g')",
            name="ck_finance_categories_name_trimmed",
        ),
        sa.CheckConstraint(
            "char_length(name) <= 100",
            name="ck_finance_categories_name_length",
        ),
        sa.CheckConstraint(
            "name_key <> ''",
            name="ck_finance_categories_name_key_not_blank",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'archived')",
            name="ck_finance_categories_status",
        ),
        sa.ForeignKeyConstraint(
            ["ledger_id"],
            ["finance_ledgers.id"],
            name="fk_finance_categories_ledger_id_finance_ledgers",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_finance_categories"),
        sa.UniqueConstraint(
            "ledger_id",
            "name_key",
            name="uq_finance_categories_ledger_name_key",
        ),
    )
    op.create_index(
        "ix_finance_categories_ledger_status_name_key_id",
        "finance_categories",
        ["ledger_id", "status", "name_key", "id"],
    )


def downgrade() -> None:
    """Drop Finance Categories."""

    op.drop_index(
        "ix_finance_categories_ledger_status_name_key_id",
        table_name="finance_categories",
    )
    op.drop_table("finance_categories")
