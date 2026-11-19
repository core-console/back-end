"""Retain effective forgiveness separately from the confirmed Allocation.

Revision ID: 20261118_01
Revises: 20261117_01
"""

import sqlalchemy as sa
from alembic import op

revision = "20261118_01"
down_revision = "20261117_01"
branch_labels = None
depends_on = None

TABLE = "finance_settlement_allocations"
CONSTRAINT = "ck_finance_settlement_allocation_effective_forgiven"
EXPENSE_LINK = "ck_finance_settlement_allocation_expense_link"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("effective_forgiven", sa.Numeric(), nullable=True))
    op.alter_column(TABLE, "transaction_id", nullable=True)
    op.create_check_constraint(
        CONSTRAINT,
        TABLE,
        "effective_forgiven IS NULL OR (kind = 'forgiveness' AND effective_forgiven >= 0 "
        "AND effective_forgiven <= forgiven)",
    )
    op.create_check_constraint(
        EXPENSE_LINK,
        TABLE,
        "(transaction_id IS NULL) = (effective_forgiven IS NOT DISTINCT FROM 0)",
    )


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM finance_settlement_allocations
                     WHERE effective_forgiven IS NOT NULL) THEN
            RAISE EXCEPTION 'Advance Corrections have restated forgiveness';
          END IF;
        END $$
    """)
    op.drop_constraint(EXPENSE_LINK, TABLE, type_="check")
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.alter_column(TABLE, "transaction_id", nullable=False)
    op.drop_column(TABLE, "effective_forgiven")
