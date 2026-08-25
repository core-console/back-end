"""Add Balance Adjustment transaction persistence."""

import sqlalchemy as sa
from alembic import op

revision = "20260825_01"
down_revision = "20260823_01"
branch_labels = None
depends_on = None


def _integrity_function(*, include_adjustment: bool) -> sa.TextClause:
    """Build the kind-aware deferred aggregate integrity function."""

    adjustment_branch = (
        """
                ELSIF transaction_kind = 'balance_adjustment' THEN
                    IF movement_count <> 1 OR allocation_count <> 0
                        OR adjustment_amount IS NULL
                    THEN
                        RAISE EXCEPTION
                            'Balance Adjustment requires one adjustment Movement only'
                            USING ERRCODE = '23514';
                    END IF;
                    IF adjustment_currency <> adjustment_account_currency
                        OR transaction_date < adjustment_tracking_start
                    THEN
                        RAISE EXCEPTION 'Movement violates Balance Adjustment semantics'
                            USING ERRCODE = '23514';
                    END IF;
    """
        if include_adjustment
        else ""
    )
    return sa.text(
        f"""
        CREATE OR REPLACE FUNCTION enforce_finance_transaction_integrity()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            affected_transaction_id uuid;
            transaction_kind text;
            transaction_date date;
            movement_count bigint;
            allocation_count bigint;
            distinct_account_count bigint;
            ordinary_amount numeric;
            ordinary_currency text;
            ordinary_nature text;
            ordinary_account_currency text;
            ordinary_tracking_start date;
            allocation_amount numeric;
            allocation_currency text;
            source_amount numeric;
            source_currency text;
            source_nature text;
            source_account_currency text;
            source_tracking_start date;
            destination_amount numeric;
            destination_currency text;
            destination_nature text;
            destination_account_currency text;
            destination_tracking_start date;
            adjustment_amount numeric;
            adjustment_currency text;
            adjustment_account_currency text;
            adjustment_tracking_start date;
            normalized_effect numeric;
            source_effect numeric;
            destination_effect numeric;
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF TG_TABLE_NAME = 'finance_transactions'
                    AND (NEW.id IS DISTINCT FROM OLD.id
                        OR NEW.ledger_id IS DISTINCT FROM OLD.ledger_id)
                THEN
                    RAISE EXCEPTION 'A Finance Transaction cannot be reparented'
                        USING ERRCODE = '23514';
                ELSIF TG_TABLE_NAME <> 'finance_transactions'
                    AND (NEW.transaction_id IS DISTINCT FROM OLD.transaction_id
                        OR NEW.ledger_id IS DISTINCT FROM OLD.ledger_id)
                THEN
                    RAISE EXCEPTION 'A Finance Transaction child cannot be reparented'
                        USING ERRCODE = '23514';
                END IF;
            END IF;

            IF TG_TABLE_NAME = 'finance_transactions' THEN
                affected_transaction_id := COALESCE(NEW.id, OLD.id);
            ELSIF TG_OP = 'DELETE' THEN
                affected_transaction_id := OLD.transaction_id;
            ELSE
                affected_transaction_id := NEW.transaction_id;
            END IF;

            SELECT kind, finance_transactions.transaction_date
            INTO transaction_kind, transaction_date
            FROM finance_transactions
            WHERE id = affected_transaction_id;
            IF NOT FOUND THEN RETURN NULL; END IF;

            PERFORM account.id
            FROM finance_account_movements AS movement
            JOIN finance_accounts AS account
              ON account.id = movement.account_id
             AND account.ledger_id = movement.ledger_id
            WHERE movement.transaction_id = affected_transaction_id
            ORDER BY account.id
            FOR UPDATE OF account;

            SELECT
                count(*), count(DISTINCT account.id),
                min(movement.amount) FILTER (WHERE movement.role = 'primary'),
                min(movement.currency) FILTER (WHERE movement.role = 'primary'),
                min(account.nature) FILTER (WHERE movement.role = 'primary'),
                min(account.currency) FILTER (WHERE movement.role = 'primary'),
                min(account.tracking_start_date) FILTER (WHERE movement.role = 'primary'),
                min(movement.amount) FILTER (WHERE movement.role = 'source'),
                min(movement.currency) FILTER (WHERE movement.role = 'source'),
                min(account.nature) FILTER (WHERE movement.role = 'source'),
                min(account.currency) FILTER (WHERE movement.role = 'source'),
                min(account.tracking_start_date) FILTER (WHERE movement.role = 'source'),
                min(movement.amount) FILTER (WHERE movement.role = 'destination'),
                min(movement.currency) FILTER (WHERE movement.role = 'destination'),
                min(account.nature) FILTER (WHERE movement.role = 'destination'),
                min(account.currency) FILTER (WHERE movement.role = 'destination'),
                min(account.tracking_start_date) FILTER (WHERE movement.role = 'destination'),
                min(movement.amount) FILTER (WHERE movement.role = 'adjustment'),
                min(movement.currency) FILTER (WHERE movement.role = 'adjustment'),
                min(account.currency) FILTER (WHERE movement.role = 'adjustment'),
                min(account.tracking_start_date) FILTER (WHERE movement.role = 'adjustment')
            INTO movement_count, distinct_account_count,
                ordinary_amount, ordinary_currency, ordinary_nature,
                ordinary_account_currency, ordinary_tracking_start,
                source_amount, source_currency, source_nature,
                source_account_currency, source_tracking_start,
                destination_amount, destination_currency, destination_nature,
                destination_account_currency, destination_tracking_start,
                adjustment_amount, adjustment_currency,
                adjustment_account_currency, adjustment_tracking_start
            FROM finance_account_movements AS movement
            JOIN finance_accounts AS account
              ON account.id = movement.account_id
             AND account.ledger_id = movement.ledger_id
            WHERE movement.transaction_id = affected_transaction_id;

            SELECT count(*), min(amount), min(currency)
            INTO allocation_count, allocation_amount, allocation_currency
            FROM finance_category_allocations
            WHERE transaction_id = affected_transaction_id;

            IF transaction_kind IN ('income', 'expense') THEN
                IF movement_count <> 1 OR allocation_count <> 1
                    OR ordinary_amount IS NULL
                THEN
                    RAISE EXCEPTION
                        'Income and Expense require one primary Movement and Allocation'
                        USING ERRCODE = '23514';
                END IF;
                normalized_effect := CASE WHEN ordinary_nature = 'asset'
                    THEN ordinary_amount ELSE -ordinary_amount END;
                IF ordinary_currency <> ordinary_account_currency
                    OR ordinary_currency <> allocation_currency
                    OR abs(normalized_effect) <> allocation_amount
                    OR transaction_date < ordinary_tracking_start
                    OR (transaction_kind = 'income' AND normalized_effect <= 0)
                    OR (transaction_kind = 'expense' AND normalized_effect >= 0)
                THEN
                    RAISE EXCEPTION
                        'Movement and Allocation violate Income or Expense semantics'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF transaction_kind = 'internal_transfer' THEN
                IF movement_count <> 2 OR distinct_account_count <> 2
                    OR allocation_count <> 0
                    OR source_amount IS NULL OR destination_amount IS NULL
                THEN
                    RAISE EXCEPTION
                        'Internal Transfer requires source and destination Movements only'
                        USING ERRCODE = '23514';
                END IF;
                source_effect := CASE WHEN source_nature = 'asset'
                    THEN source_amount ELSE -source_amount END;
                destination_effect := CASE WHEN destination_nature = 'asset'
                    THEN destination_amount ELSE -destination_amount END;
                IF source_currency <> source_account_currency
                    OR destination_currency <> destination_account_currency
                    OR source_currency <> destination_currency
                    OR abs(source_effect) <> abs(destination_effect)
                    OR source_effect >= 0 OR destination_effect <= 0
                    OR transaction_date < source_tracking_start
                    OR transaction_date < destination_tracking_start
                THEN
                    RAISE EXCEPTION 'Movements violate Internal Transfer semantics'
                        USING ERRCODE = '23514';
                END IF;
            {adjustment_branch}
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )


def upgrade() -> None:
    """Admit the fourth kind and its one-Movement aggregate."""

    op.execute(
        sa.text(
            "ALTER FUNCTION enforce_finance_ordinary_transaction_integrity() "
            "RENAME TO enforce_finance_transaction_integrity"
        )
    )
    op.drop_constraint("ck_finance_transactions_kind", "finance_transactions", type_="check")
    op.create_check_constraint(
        "ck_finance_transactions_kind",
        "finance_transactions",
        "kind IN ('income', 'expense', 'internal_transfer', 'balance_adjustment')",
    )
    op.drop_constraint(
        "ck_finance_account_movements_role",
        "finance_account_movements",
        type_="check",
    )
    op.create_check_constraint(
        "ck_finance_account_movements_role",
        "finance_account_movements",
        "role IN ('primary', 'source', 'destination', 'adjustment')",
    )
    op.execute(_integrity_function(include_adjustment=True))


def downgrade() -> None:
    """Remove Balance Adjustment support only when no such history exists."""

    op.execute(
        sa.text(
            """
            DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM finance_transactions WHERE kind = 'balance_adjustment')
                THEN RAISE EXCEPTION 'Cannot downgrade while Balance Adjustments exist';
                END IF;
            END $$
            """
        )
    )
    op.execute(_integrity_function(include_adjustment=False))
    op.execute(
        sa.text(
            "ALTER FUNCTION enforce_finance_transaction_integrity() "
            "RENAME TO enforce_finance_ordinary_transaction_integrity"
        )
    )
    op.drop_constraint(
        "ck_finance_account_movements_role",
        "finance_account_movements",
        type_="check",
    )
    op.create_check_constraint(
        "ck_finance_account_movements_role",
        "finance_account_movements",
        "role IN ('primary', 'source', 'destination')",
    )
    op.drop_constraint("ck_finance_transactions_kind", "finance_transactions", type_="check")
    op.create_check_constraint(
        "ck_finance_transactions_kind",
        "finance_transactions",
        "kind IN ('income', 'expense', 'internal_transfer')",
    )
