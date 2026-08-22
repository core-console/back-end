"""Create Income and Expense transaction persistence."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260822_01"
down_revision = "20260820_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create atomic Transaction, Account Movement, and Category Allocation state."""

    op.create_unique_constraint(
        "uq_finance_accounts_id_ledger_id",
        "finance_accounts",
        ["id", "ledger_id"],
    )
    op.create_unique_constraint(
        "uq_finance_categories_id_ledger_id",
        "finance_categories",
        ["id", "ledger_id"],
    )
    op.create_table(
        "finance_transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ledger_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("transaction_date", sa.Date(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
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
            "kind IN ('income', 'expense')",
            name="ck_finance_transactions_kind",
        ),
        sa.CheckConstraint(
            "note IS NULL OR (note = btrim(note) AND char_length(note) BETWEEN 1 AND 500)",
            name="ck_finance_transactions_note",
        ),
        sa.ForeignKeyConstraint(
            ["ledger_id"],
            ["finance_ledgers.id"],
            name="fk_finance_transactions_ledger_id_finance_ledgers",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_finance_transactions"),
        sa.UniqueConstraint(
            "id",
            "ledger_id",
            name="uq_finance_transactions_id_ledger_id",
        ),
    )
    op.create_index(
        "ix_finance_transactions_ledger_date_id",
        "finance_transactions",
        ["ledger_id", "transaction_date", "id"],
    )
    op.create_table(
        "finance_account_movements",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ledger_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount", sa.Numeric(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "amount NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name="ck_finance_account_movements_amount_finite",
        ),
        sa.CheckConstraint(
            "amount <> 0",
            name="ck_finance_account_movements_amount_nonzero",
        ),
        sa.CheckConstraint(
            "currency IN ('CNY', 'JPY', 'USD')",
            name="ck_finance_account_movements_currency",
        ),
        sa.CheckConstraint(
            "(currency = 'JPY' AND scale(amount) = 0) OR "
            "(currency IN ('CNY', 'USD') AND scale(amount) <= 2)",
            name="ck_finance_account_movements_amount_scale",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "ledger_id"],
            ["finance_accounts.id", "finance_accounts.ledger_id"],
            name="fk_finance_account_movements_account_ledger",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id", "ledger_id"],
            ["finance_transactions.id", "finance_transactions.ledger_id"],
            name="fk_finance_account_movements_transaction_ledger",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_finance_account_movements"),
    )
    op.create_index(
        "ix_finance_account_movements_account_transaction",
        "finance_account_movements",
        ["account_id", "transaction_id"],
    )
    op.create_table(
        "finance_category_allocations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ledger_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("amount", sa.Numeric(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "amount NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name="ck_finance_category_allocations_amount_finite",
        ),
        sa.CheckConstraint(
            "amount > 0",
            name="ck_finance_category_allocations_amount_positive",
        ),
        sa.CheckConstraint(
            "currency IN ('CNY', 'JPY', 'USD')",
            name="ck_finance_category_allocations_currency",
        ),
        sa.CheckConstraint(
            "(currency = 'JPY' AND scale(amount) = 0) OR "
            "(currency IN ('CNY', 'USD') AND scale(amount) <= 2)",
            name="ck_finance_category_allocations_amount_scale",
        ),
        sa.ForeignKeyConstraint(
            ["category_id", "ledger_id"],
            ["finance_categories.id", "finance_categories.ledger_id"],
            name="fk_finance_category_allocations_category_ledger",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id", "ledger_id"],
            ["finance_transactions.id", "finance_transactions.ledger_id"],
            name="fk_finance_category_allocations_transaction_ledger",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_finance_category_allocations"),
        sa.UniqueConstraint(
            "transaction_id",
            name="uq_finance_category_allocations_transaction_id",
        ),
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION enforce_finance_ordinary_transaction_integrity()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            DECLARE
                affected_transaction_id uuid;
                transaction_kind text;
                movement_count bigint;
                movement_amount numeric;
                movement_currency text;
                account_nature text;
                account_currency text;
                account_tracking_start_date date;
                allocation_count bigint;
                allocation_amount numeric;
                allocation_currency text;
                normalized_effect numeric;
                transaction_date date;
            BEGIN
                IF TG_OP = 'UPDATE' THEN
                    IF TG_TABLE_NAME = 'finance_transactions'
                        AND (NEW.id IS DISTINCT FROM OLD.id
                            OR NEW.ledger_id IS DISTINCT FROM OLD.ledger_id)
                    THEN
                        RAISE EXCEPTION
                            'A Finance Transaction cannot be reparented'
                            USING ERRCODE = '23514';
                    ELSIF TG_TABLE_NAME <> 'finance_transactions'
                        AND (NEW.transaction_id IS DISTINCT FROM OLD.transaction_id
                            OR NEW.ledger_id IS DISTINCT FROM OLD.ledger_id)
                    THEN
                        RAISE EXCEPTION
                            'A Finance Transaction child cannot be reparented'
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

                IF NOT FOUND THEN
                    RETURN NULL;
                END IF;

                PERFORM account.id
                FROM finance_account_movements AS movement
                JOIN finance_accounts AS account
                    ON account.id = movement.account_id
                    AND account.ledger_id = movement.ledger_id
                WHERE movement.transaction_id = affected_transaction_id
                ORDER BY account.id
                FOR UPDATE OF account;

                SELECT
                    count(*),
                    min(movement.amount),
                    min(movement.currency),
                    min(account.nature),
                    min(account.currency),
                    min(account.tracking_start_date)
                INTO
                    movement_count,
                    movement_amount,
                    movement_currency,
                    account_nature,
                    account_currency,
                    account_tracking_start_date
                FROM finance_account_movements AS movement
                JOIN finance_accounts AS account ON account.id = movement.account_id
                WHERE movement.transaction_id = affected_transaction_id;

                SELECT count(*), min(amount), min(currency)
                INTO allocation_count, allocation_amount, allocation_currency
                FROM finance_category_allocations
                WHERE transaction_id = affected_transaction_id;

                IF movement_count <> 1 OR allocation_count <> 1 THEN
                    RAISE EXCEPTION
                        'Income and Expense require exactly one Movement and Allocation'
                        USING ERRCODE = '23514';
                END IF;

                normalized_effect := CASE
                    WHEN account_nature = 'asset' THEN movement_amount
                    ELSE -movement_amount
                END;
                IF movement_currency <> account_currency
                    OR movement_currency <> allocation_currency
                    OR abs(normalized_effect) <> allocation_amount
                    OR transaction_date < account_tracking_start_date
                    OR (transaction_kind = 'income' AND normalized_effect <= 0)
                    OR (transaction_kind = 'expense' AND normalized_effect >= 0)
                THEN
                    RAISE EXCEPTION
                        'Movement and Allocation violate Income or Expense semantics'
                        USING ERRCODE = '23514';
                END IF;

                RETURN NULL;
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION enforce_finance_account_tracking_start_integrity()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            DECLARE
                earliest_transaction_date date;
            BEGIN
                SELECT min(finance_transaction.transaction_date)
                INTO earliest_transaction_date
                FROM finance_transactions AS finance_transaction
                JOIN finance_account_movements AS movement
                    ON movement.transaction_id = finance_transaction.id
                WHERE movement.account_id = NEW.id;

                IF earliest_transaction_date IS NOT NULL
                    AND NEW.tracking_start_date > earliest_transaction_date
                THEN
                    RAISE EXCEPTION
                        'Tracking Start Date cannot exclude Account Transaction history'
                        USING ERRCODE = '23514';
                END IF;

                RETURN NULL;
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION enforce_finance_account_semantics_lock()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            DECLARE
                current_opening_balance numeric;
                has_account_movement boolean;
            BEGIN
                SELECT opening_balance
                INTO current_opening_balance
                FROM finance_accounts
                WHERE id = NEW.id;

                IF NOT FOUND THEN
                    RETURN NULL;
                END IF;

                SELECT EXISTS (
                    SELECT 1
                    FROM finance_account_movements
                    WHERE account_id = NEW.id
                )
                INTO has_account_movement;

                IF current_opening_balance <> 0 OR has_account_movement THEN
                    RAISE EXCEPTION
                        'Account Nature and Currency are locked by its financial position'
                        USING ERRCODE = '23514';
                END IF;

                RETURN NULL;
            END;
            $$
            """
        )
    )
    for table_name in (
        "finance_transactions",
        "finance_account_movements",
        "finance_category_allocations",
    ):
        op.execute(
            sa.text(
                f"""
                CREATE CONSTRAINT TRIGGER ck_{table_name}_ordinary_integrity
                AFTER INSERT OR UPDATE OR DELETE ON {table_name}
                DEFERRABLE INITIALLY DEFERRED
                FOR EACH ROW
                EXECUTE FUNCTION enforce_finance_ordinary_transaction_integrity()
                """
            )
        )
    op.execute(
        sa.text(
            """
            CREATE CONSTRAINT TRIGGER ck_finance_accounts_tracking_start_integrity
            AFTER UPDATE OF tracking_start_date ON finance_accounts
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            EXECUTE FUNCTION enforce_finance_account_tracking_start_integrity()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE CONSTRAINT TRIGGER ck_finance_accounts_semantics_lock
            AFTER UPDATE OF nature, currency ON finance_accounts
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            WHEN (OLD.nature IS DISTINCT FROM NEW.nature
                OR OLD.currency IS DISTINCT FROM NEW.currency)
            EXECUTE FUNCTION enforce_finance_account_semantics_lock()
            """
        )
    )


def downgrade() -> None:
    """Drop Income and Expense transaction persistence."""

    op.execute(sa.text("DROP FUNCTION enforce_finance_account_semantics_lock() CASCADE"))
    op.execute(sa.text("DROP FUNCTION enforce_finance_account_tracking_start_integrity() CASCADE"))
    op.execute(sa.text("DROP FUNCTION enforce_finance_ordinary_transaction_integrity() CASCADE"))
    op.drop_table("finance_category_allocations")
    op.drop_index(
        "ix_finance_account_movements_account_transaction",
        table_name="finance_account_movements",
    )
    op.drop_table("finance_account_movements")
    op.drop_index(
        "ix_finance_transactions_ledger_date_id",
        table_name="finance_transactions",
    )
    op.drop_table("finance_transactions")
    op.drop_constraint(
        "uq_finance_categories_id_ledger_id",
        "finance_categories",
        type_="unique",
    )
    op.drop_constraint(
        "uq_finance_accounts_id_ledger_id",
        "finance_accounts",
        type_="unique",
    )
