"""Add durable Ledger submission bindings without historical backfill."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261003_01"
down_revision = "20260825_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add binding consistency, immutable evidence, and ownership retention."""
    op.create_unique_constraint(
        "uq_finance_ledgers_id_owner", "finance_ledgers", ["id", "owner_id"]
    )
    op.create_table(
        "finance_submissions",
        sa.Column("local_user_id", postgresql.UUID(), nullable=False),
        sa.Column("submission_id", postgresql.UUID(), nullable=False),
        sa.Column("command_version", sa.Text(), nullable=False),
        sa.Column("canonical_command", postgresql.JSONB(), nullable=False),
        sa.Column("retention_ledger_id", postgresql.UUID(), nullable=True),
        sa.Column(
            "admitted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("terminal_outcome", postgresql.JSONB(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("local_user_id", "submission_id", name="pk_finance_submissions"),
        sa.ForeignKeyConstraint(
            ["local_user_id"], ["users.id"], name="fk_finance_submissions_user"
        ),
        sa.ForeignKeyConstraint(
            ["retention_ledger_id", "local_user_id"],
            ["finance_ledgers.id", "finance_ledgers.owner_id"],
            name="fk_finance_submissions_retention_owner",
        ),
        sa.CheckConstraint(
            """
            command_version = '1'
            AND canonical_command = jsonb_build_object(
              'commandVersion', command_version, 'operation', 'createFinanceLedger',
              'targetLedgerId', NULL, 'body',
              jsonb_build_object('name', canonical_command #> '{body,name}'))
            AND jsonb_typeof(canonical_command #> '{body,name}') = 'string'
            AND char_length(canonical_command #>> '{body,name}') BETWEEN 1 AND 100
            AND canonical_command #>> '{body,name}' = regexp_replace(
                canonical_command #>> '{body,name}', '^[[:space:]]+|[[:space:]]+$', '', 'g')
        """,
            name="ck_finance_submissions_command",
        ),
        sa.CheckConstraint(
            """
          (terminal_outcome IS NULL AND resolved_at IS NULL AND retention_ledger_id IS NULL)
          OR (terminal_outcome IS NOT NULL AND resolved_at IS NOT NULL
            AND resolved_at >= admitted_at
            AND (
              (terminal_outcome = jsonb_build_object('kind', 'created', 'resource',
                jsonb_build_object('type', 'ledger', 'id', retention_ledger_id::text))
               AND retention_ledger_id IS NOT NULL)
              OR (retention_ledger_id IS NULL
                AND terminal_outcome = jsonb_build_object('kind', 'rejected', 'problem',
                  jsonb_build_object('type', 'about:blank', 'title', 'Conflict', 'status', 409,
                    'code', 'finance_ledger_name_conflict',
                    'detail', 'A Finance Ledger with this name already exists.')))
            ))
        """,
            name="ck_finance_submissions_outcome",
        ),
    )
    op.execute("""
      CREATE FUNCTION protect_finance_submission() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF ROW(NEW.local_user_id, NEW.submission_id, NEW.command_version,
               NEW.canonical_command, NEW.admitted_at)
           IS DISTINCT FROM ROW(OLD.local_user_id, OLD.submission_id, OLD.command_version,
               OLD.canonical_command, OLD.admitted_at)
           OR (OLD.terminal_outcome IS NOT NULL AND NEW IS DISTINCT FROM OLD)
        THEN RAISE EXCEPTION 'Finance submission evidence is immutable' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER finance_submission_immutable BEFORE UPDATE ON finance_submissions
      FOR EACH ROW EXECUTE FUNCTION protect_finance_submission();
    """)


def downgrade() -> None:
    """Allow empty development round trips; refuse to erase keyed evidence."""
    op.execute("LOCK TABLE finance_submissions IN ACCESS EXCLUSIVE MODE")
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM finance_submissions) THEN
          RAISE EXCEPTION 'Finance submission enforcement must be preserved; repair forward';
        END IF;
      END $$;
    """)
    op.drop_table("finance_submissions")
    op.execute("DROP FUNCTION protect_finance_submission()")
    op.drop_constraint("uq_finance_ledgers_id_owner", "finance_ledgers", type_="unique")
