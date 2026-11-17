"""Retain protected receipt reclassifications from Advance Corrections.

Revision ID: 20261117_01
Revises: 20261102_01
"""

import sqlalchemy as sa
from alembic import op

revision = "20261117_01"
down_revision = "20261102_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_finance_advance_corrections_ledger", "finance_advance_corrections", ["id", "ledger_id"]
    )
    op.create_table(
        "finance_advance_correction_transactions",
        sa.Column("transaction_id", sa.Uuid(), primary_key=True),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("correction_id", sa.Uuid(), nullable=False),
        sa.Column("settlement_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["correction_id", "ledger_id"],
            ["finance_advance_corrections.id", "finance_advance_corrections.ledger_id"],
        ),
        sa.ForeignKeyConstraint(
            ["settlement_id", "ledger_id"],
            ["finance_settlements.id", "finance_settlements.ledger_id"],
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id", "ledger_id"],
            ["finance_transactions.id", "finance_transactions.ledger_id"],
        ),
    )


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM finance_advance_correction_transactions)
             OR EXISTS (SELECT 1 FROM finance_advance_corrections WHERE plan ? 'cashReleases') THEN
            RAISE EXCEPTION 'Advance correction evidence includes retained cash Correction plans';
          END IF;
        END $$
    """)
    op.drop_table("finance_advance_correction_transactions")
    op.drop_constraint(
        "uq_finance_advance_corrections_ledger", "finance_advance_corrections", type_="unique"
    )
