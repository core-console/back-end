"""Extend retained Finance evidence to Account and Category creates."""

from alembic import op

revision = "20261004_01"
down_revision = "20261003_01"
branch_labels = None
depends_on = None

NEW_COMMAND = r"""
COALESCE(command_version = '1'
AND canonical_command = jsonb_build_object(
    'commandVersion', command_version, 'operation', canonical_command->>'operation',
    'targetLedgerId', canonical_command->'targetLedgerId', 'body', canonical_command->'body')
AND jsonb_typeof(canonical_command #> '{body,name}') = 'string'
AND char_length(canonical_command #>> '{body,name}') BETWEEN 1 AND 100
AND canonical_command #>> '{body,name}' = regexp_replace(
    canonical_command #>> '{body,name}', '^[[:space:]]+|[[:space:]]+$', '', 'g')
AND (
  (canonical_command->>'operation' = 'createFinanceLedger'
   AND canonical_command->'targetLedgerId' = 'null'::jsonb
   AND canonical_command->'body' = jsonb_build_object(
         'name', canonical_command #> '{body,name}'))
  OR (
    canonical_command->>'operation' IN ('createFinanceAccount', 'createFinanceCategory')
    AND jsonb_typeof(canonical_command->'targetLedgerId') = 'string'
    AND canonical_command->>'targetLedgerId' ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
    AND (
      (canonical_command->>'operation' = 'createFinanceCategory'
       AND canonical_command->'body' = jsonb_build_object(
         'name', canonical_command #> '{body,name}'))
      OR (canonical_command->>'operation' = 'createFinanceAccount'
        AND canonical_command->'body' = jsonb_build_object(
          'name', canonical_command #> '{body,name}',
          'nature', canonical_command #> '{body,nature}',
          'currency', canonical_command #> '{body,currency}',
          'openingBalance', canonical_command #> '{body,openingBalance}',
          'trackingStartDate', canonical_command #> '{body,trackingStartDate}')
        AND canonical_command #>> '{body,nature}' IN ('asset', 'liability')
        AND canonical_command #>> '{body,currency}' IN ('CNY', 'JPY', 'USD')
        AND canonical_command #> '{body,openingBalance}' = jsonb_build_object(
          'amount', canonical_command #> '{body,openingBalance,amount}',
          'currency', canonical_command #> '{body,currency}')
        AND jsonb_typeof(canonical_command #> '{body,openingBalance,amount}') = 'string'
        AND (CASE WHEN canonical_command #>> '{body,currency}' = 'JPY'
             THEN canonical_command #>> '{body,openingBalance,amount}' ~ '^-?(0|[1-9][0-9]*)$'
             ELSE canonical_command #>> '{body,openingBalance,amount}'
               ~ '^-?(0|[1-9][0-9]*)[.][0-9]{2}$' END)
        AND canonical_command #>> '{body,openingBalance,amount}' NOT IN ('-0', '-0.00')
        AND char_length(split_part(
            ltrim(canonical_command #>> '{body,openingBalance,amount}', '-'), '.', 1)) <= 131072
        AND jsonb_typeof(canonical_command #> '{body,trackingStartDate}') = 'string'
        AND canonical_command #>> '{body,trackingStartDate}'
            ~ '^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$'
        AND (canonical_command #>> '{body,trackingStartDate}')::date
            BETWEEN DATE '0001-01-01' AND DATE '9999-12-31'
      )
    )
  )
), FALSE)
"""
NEW_OUTCOME = r"""
COALESCE(
(
  (canonical_command->>'operation' = 'createFinanceLedger')
  OR (retention_ledger_id IS NOT NULL
      AND retention_ledger_id::text = canonical_command->>'targetLedgerId')
)
AND (
  (terminal_outcome IS NULL AND resolved_at IS NULL
    AND (canonical_command->>'operation' <> 'createFinanceLedger'
             OR retention_ledger_id IS NULL))
  OR (terminal_outcome IS NOT NULL AND resolved_at IS NOT NULL AND resolved_at >= admitted_at
    AND (
      (terminal_outcome = jsonb_build_object('kind', 'created', 'resource', jsonb_build_object(
        'type', CASE canonical_command->>'operation'
          WHEN 'createFinanceLedger' THEN 'ledger'
          WHEN 'createFinanceAccount' THEN 'account'
          WHEN 'createFinanceCategory' THEN 'category' END,
        'id', terminal_outcome #> '{resource,id}'))
       AND jsonb_typeof(terminal_outcome #> '{resource,id}') = 'string'
       AND terminal_outcome #>> '{resource,id}' ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
       AND retention_ledger_id IS NOT NULL
       AND (canonical_command->>'operation' <> 'createFinanceLedger'
            OR terminal_outcome #>> '{resource,id}' = retention_ledger_id::text))
      OR (canonical_command->>'operation' IN ('createFinanceLedger', 'createFinanceCategory')
        AND (canonical_command->>'operation' <> 'createFinanceLedger'
             OR retention_ledger_id IS NULL)
        AND terminal_outcome = jsonb_build_object('kind', 'rejected', 'problem',
          jsonb_build_object('type', 'about:blank', 'title', 'Conflict', 'status', 409,
            'code', CASE canonical_command->>'operation'
              WHEN 'createFinanceLedger' THEN 'finance_ledger_name_conflict'
              ELSE 'finance_category_name_conflict' END,
            'detail', CASE canonical_command->>'operation'
              WHEN 'createFinanceLedger' THEN 'A Finance Ledger with this name already exists.'
              ELSE 'A Finance Category with this name already exists.' END)))
      OR (canonical_command->>'operation' = 'createFinanceAccount'
        AND jsonb_typeof(terminal_outcome #> '{problem,detail}') = 'string'
        AND terminal_outcome = jsonb_build_object('kind', 'rejected', 'problem',
          jsonb_build_object('type', 'about:blank', 'title', 'Unprocessable Entity', 'status', 422,
            'code', 'validation_error', 'detail', terminal_outcome #> '{problem,detail}')))
    ))
), FALSE)
"""
OLD_COMMAND = """
command_version = '1'
AND canonical_command = jsonb_build_object(
    'commandVersion', command_version, 'operation', 'createFinanceLedger',
    'targetLedgerId', NULL, 'body', jsonb_build_object('name', canonical_command #> '{body,name}'))
AND jsonb_typeof(canonical_command #> '{body,name}') = 'string'
AND char_length(canonical_command #>> '{body,name}') BETWEEN 1 AND 100
AND canonical_command #>> '{body,name}' = regexp_replace(
    canonical_command #>> '{body,name}', '^[[:space:]]+|[[:space:]]+$', '', 'g')
"""
OLD_OUTCOME = """
(terminal_outcome IS NULL AND resolved_at IS NULL AND retention_ledger_id IS NULL)
OR (terminal_outcome IS NOT NULL AND resolved_at IS NOT NULL AND resolved_at >= admitted_at
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
"""


def _replace(command: str, outcome: str) -> None:
    op.drop_constraint("ck_finance_submissions_command", "finance_submissions", type_="check")
    op.drop_constraint("ck_finance_submissions_outcome", "finance_submissions", type_="check")
    op.create_check_constraint("ck_finance_submissions_command", "finance_submissions", command)
    op.create_check_constraint("ck_finance_submissions_outcome", "finance_submissions", outcome)


def upgrade() -> None:
    _replace(NEW_COMMAND, NEW_OUTCOME)


def downgrade() -> None:
    # Do not remove nested enforcement while any nested evidence remains.
    op.execute("LOCK TABLE finance_submissions IN ACCESS EXCLUSIVE MODE")
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM finance_submissions
                   WHERE canonical_command->>'operation' <> 'createFinanceLedger') THEN
          RAISE EXCEPTION 'Finance submission enforcement must be preserved; repair forward';
        END IF;
      END $$;
    """)
    _replace(OLD_COMMAND, OLD_OUTCOME)
