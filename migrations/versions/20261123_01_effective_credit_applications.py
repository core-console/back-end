"""Track effective Credit applications alongside confirmed Allocations."""

import sqlalchemy as sa
from alembic import op

revision = "20261123_01"
down_revision = "20261122_01"
branch_labels = None
depends_on = None

TABLE = "finance_credit_application_allocations"
CHECK = "ck_finance_credit_application_effective_amount"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("effective_amount", sa.Numeric(), nullable=True))
    op.create_check_constraint(CHECK, TABLE, "effective_amount BETWEEN 0 AND amount")


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM finance_credit_application_allocations
                     WHERE effective_amount IS NOT NULL) THEN
            RAISE EXCEPTION 'Advance Corrections have restored applied Credit';
          END IF;
        END $$
    """)
    op.drop_constraint(CHECK, TABLE, type_="check")
    op.drop_column(TABLE, "effective_amount")
