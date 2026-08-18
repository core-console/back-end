"""Create personally owned Finance Ledgers."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260818_01"
down_revision = "20260802_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create Finance Ledgers and their ownership/name invariants."""

    op.create_table(
        "finance_ledgers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("name_key", sa.Text(collation="C"), nullable=False),
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
            name="ck_finance_ledgers_name_not_blank",
        ),
        sa.CheckConstraint(
            "name = regexp_replace(name, '^[[:space:]]+|[[:space:]]+$', '', 'g')",
            name="ck_finance_ledgers_name_trimmed",
        ),
        sa.CheckConstraint(
            "char_length(name) <= 100",
            name="ck_finance_ledgers_name_length",
        ),
        sa.CheckConstraint(
            "name_key <> ''",
            name="ck_finance_ledgers_name_key_not_blank",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name="fk_finance_ledgers_owner_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_finance_ledgers"),
        sa.UniqueConstraint(
            "owner_id",
            "name_key",
            name="uq_finance_ledgers_owner_name_key",
        ),
    )


def downgrade() -> None:
    """Drop Finance Ledgers."""

    op.drop_table("finance_ledgers")
