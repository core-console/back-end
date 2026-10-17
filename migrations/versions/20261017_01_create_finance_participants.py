"""Create Ledger-owned Finance Participants."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261017_01"
down_revision = "20261006_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create private identities without display-name uniqueness or financial effects."""

    op.create_table(
        "finance_participants",
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
            name="ck_finance_participants_name_not_blank",
        ),
        sa.CheckConstraint(
            "name = regexp_replace(name, '^[[:space:]]+|[[:space:]]+$', '', 'g')",
            name="ck_finance_participants_name_trimmed",
        ),
        sa.CheckConstraint(
            "char_length(name) <= 100",
            name="ck_finance_participants_name_length",
        ),
        sa.CheckConstraint(
            "name_key <> ''",
            name="ck_finance_participants_name_key_not_blank",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'archived')",
            name="ck_finance_participants_status",
        ),
        sa.ForeignKeyConstraint(
            ["ledger_id"],
            ["finance_ledgers.id"],
            name="fk_finance_participants_ledger_id_finance_ledgers",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_finance_participants"),
        sa.UniqueConstraint(
            "id",
            "ledger_id",
            name="uq_finance_participants_id_ledger_id",
        ),
    )
    op.create_index(
        "ix_finance_participants_ledger_status_name_key_id",
        "finance_participants",
        ["ledger_id", "status", "name_key", "id"],
    )


def downgrade() -> None:
    """Drop Finance Participants."""

    op.drop_index(
        "ix_finance_participants_ledger_status_name_key_id",
        table_name="finance_participants",
    )
    op.drop_table("finance_participants")
