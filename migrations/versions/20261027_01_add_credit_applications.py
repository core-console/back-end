"""Retain explicit credit application provenance and closed v7 recovery evidence."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261027_01"
down_revision = "20261025_01"
branch_labels = None
depends_on = None

OLD_COMMAND = r"""
(
(
((((
(((COALESCE(command_version = '1'
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
), FALSE)) OR (COALESCE(command_version = '1'
AND canonical_command = jsonb_build_object(
    'commandVersion', command_version, 'operation', 'createFinanceTransaction',
    'targetLedgerId', canonical_command->'targetLedgerId', 'body', canonical_command->'body')
AND jsonb_typeof(canonical_command->'targetLedgerId') = 'string'
AND canonical_command->>'targetLedgerId' ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
AND jsonb_typeof(canonical_command #> '{body,transactionDate}') = 'string'
AND canonical_command #>> '{body,transactionDate}' ~
    '^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$'
AND (canonical_command #>> '{body,transactionDate}')::date BETWEEN DATE '0001-01-01' AND DATE
    '9999-12-31'
AND (canonical_command #> '{body,note}' = 'null'::jsonb
    OR (jsonb_typeof(canonical_command #> '{body,note}') = 'string'
        AND char_length(canonical_command #>> '{body,note}') BETWEEN 1 AND 500
        AND canonical_command #>> '{body,note}' = regexp_replace(canonical_command #>>
            '{body,note}', '^[[:space:]]+|[[:space:]]+$', '', 'g')))
AND (
    (canonical_command #>> '{body,kind}' IN ('income', 'expense')
    AND canonical_command->'body' = jsonb_build_object('kind', canonical_command #> '{body,kind}',
        'accountId', canonical_command #> '{body,accountId}',
        'transactionDate', canonical_command #> '{body,transactionDate}',
        'economicAmount', canonical_command #> '{body,economicAmount}',
        'categoryAllocations', canonical_command #> '{body,categoryAllocations}',
        'note', canonical_command #> '{body,note}')
    AND (jsonb_typeof(canonical_command #> '{body,accountId}') = 'string' AND canonical_command
        #>> '{body,accountId}' ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$')
    AND (canonical_command #> '{body,economicAmount}' = jsonb_build_object('amount',
        canonical_command #> '{body,economicAmount,amount}', 'currency', canonical_command #>
        '{body,economicAmount,currency}')
    AND jsonb_typeof(canonical_command #> '{body,economicAmount,amount}') = 'string'
    AND canonical_command #>> '{body,economicAmount,currency}' IN ('CNY', 'USD', 'JPY')
    AND CASE WHEN canonical_command #>> '{body,economicAmount,currency}' = 'JPY' THEN
        canonical_command #>> '{body,economicAmount,amount}' ~ '^(0|[1-9][0-9]*)$'
        ELSE canonical_command #>> '{body,economicAmount,amount}' ~
            '^(0|[1-9][0-9]*)[.][0-9]{2}$' END
    AND canonical_command #>> '{body,economicAmount,amount}' NOT IN ('0', '0.00')
    AND char_length(split_part(canonical_command #>> '{body,economicAmount,amount}', '.', 1))
        <= 131072)
    AND canonical_command #> '{body,categoryAllocations}' = jsonb_build_array(jsonb_build_object(
        'amount', canonical_command #> '{body,economicAmount}', 'categoryId',
            canonical_command #> '{body,categoryAllocations,0,categoryId}'))
    AND (canonical_command #> '{body,categoryAllocations,0,categoryId}' = 'null'::jsonb
        OR (jsonb_typeof(canonical_command #> '{body,categoryAllocations,0,categoryId}') =
            'string' AND canonical_command #>> '{body,categoryAllocations,0,categoryId}' ~
            '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$')))
    OR (canonical_command #>> '{body,kind}' = 'internalTransfer'
    AND canonical_command->'body' = jsonb_build_object('kind', canonical_command #> '{body,kind}',
        'sourceAccountId', canonical_command #> '{body,sourceAccountId}',
        'destinationAccountId', canonical_command #> '{body,destinationAccountId}',
        'amount', canonical_command #> '{body,amount}',
        'transactionDate', canonical_command #> '{body,transactionDate}',
        'note', canonical_command #> '{body,note}')
    AND (jsonb_typeof(canonical_command #> '{body,sourceAccountId}') = 'string' AND
        canonical_command #>> '{body,sourceAccountId}' ~
        '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$')
    AND (jsonb_typeof(canonical_command #> '{body,destinationAccountId}') = 'string' AND
        canonical_command #>> '{body,destinationAccountId}' ~
        '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$')
    AND canonical_command #> '{body,sourceAccountId}' <> canonical_command #>
        '{body,destinationAccountId}'
    AND (canonical_command #> '{body,amount}' = jsonb_build_object('amount', canonical_command #>
        '{body,amount,amount}', 'currency', canonical_command #> '{body,amount,currency}')
    AND jsonb_typeof(canonical_command #> '{body,amount,amount}') = 'string'
    AND canonical_command #>> '{body,amount,currency}' IN ('CNY', 'USD', 'JPY')
    AND CASE WHEN canonical_command #>> '{body,amount,currency}' = 'JPY' THEN canonical_command
        #>> '{body,amount,amount}' ~ '^(0|[1-9][0-9]*)$'
        ELSE canonical_command #>> '{body,amount,amount}' ~ '^(0|[1-9][0-9]*)[.][0-9]{2}$' END
    AND canonical_command #>> '{body,amount,amount}' NOT IN ('0', '0.00')
    AND char_length(split_part(canonical_command #>> '{body,amount,amount}', '.', 1)) <= 131072))
), FALSE))) OR (COALESCE((command_version = '1'
AND canonical_command = jsonb_build_object(
    'commandVersion', command_version, 'operation', 'createBalanceAdjustment',
    'targetLedgerId', canonical_command->'targetLedgerId', 'body', canonical_command->'body')
AND jsonb_typeof(canonical_command->'targetLedgerId') = 'string'
AND canonical_command->>'targetLedgerId' ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
AND canonical_command->'body' = jsonb_build_object(
    'accountId', canonical_command #> '{body,accountId}',
    'transactionDate', canonical_command #> '{body,transactionDate}',
    'expectedDerivedBalance', canonical_command #> '{body,expectedDerivedBalance}',
    'expectedAccountNature', canonical_command #> '{body,expectedAccountNature}',
    'targetBalance', canonical_command #> '{body,targetBalance}',
    'note', canonical_command #> '{body,note}')
AND jsonb_typeof(canonical_command #> '{body,accountId}') = 'string'
AND canonical_command #>> '{body,accountId}' ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
AND canonical_command #>> '{body,expectedAccountNature}' IN ('asset', 'liability')
AND jsonb_typeof(canonical_command #> '{body,transactionDate}') = 'string'
AND canonical_command #>> '{body,transactionDate}' ~
    '^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$'
AND (canonical_command #>> '{body,transactionDate}')::date
    BETWEEN DATE '0001-01-01' AND DATE '9999-12-31'
AND (canonical_command #> '{body,note}' = 'null'::jsonb
    OR (jsonb_typeof(canonical_command #> '{body,note}') = 'string'
        AND char_length(canonical_command #>> '{body,note}') BETWEEN 1 AND 500
        AND canonical_command #>> '{body,note}' = regexp_replace(
            canonical_command #>> '{body,note}', '^[[:space:]]+|[[:space:]]+$', '', 'g')))

AND canonical_command #> '{body,expectedDerivedBalance}' = jsonb_build_object(
    'amount', canonical_command #> '{body,expectedDerivedBalance,amount}',
    'currency', canonical_command #> '{body,expectedDerivedBalance,currency}')
AND jsonb_typeof(canonical_command #> '{body,expectedDerivedBalance,amount}') = 'string'
AND canonical_command #>> '{body,expectedDerivedBalance,currency}' IN ('CNY', 'USD', 'JPY')
AND CASE WHEN canonical_command #>> '{body,expectedDerivedBalance,currency}' = 'JPY' THEN
    canonical_command #>> '{body,expectedDerivedBalance,amount}' ~ '^-?(0|[1-9][0-9]*)$'
    ELSE canonical_command #>> '{body,expectedDerivedBalance,amount}'
        ~ '^-?(0|[1-9][0-9]*)[.][0-9]{2}$' END
AND canonical_command #>> '{body,expectedDerivedBalance,amount}' NOT IN ('-0', '-0.00')
AND char_length(split_part(
    ltrim(canonical_command #>> '{body,expectedDerivedBalance,amount}', '-'), '.', 1)) <= 131072

AND canonical_command #> '{body,targetBalance}' = jsonb_build_object(
    'amount', canonical_command #> '{body,targetBalance,amount}',
    'currency', canonical_command #> '{body,targetBalance,currency}')
AND jsonb_typeof(canonical_command #> '{body,targetBalance,amount}') = 'string'
AND canonical_command #>> '{body,targetBalance,currency}' IN ('CNY', 'USD', 'JPY')
AND CASE WHEN canonical_command #>> '{body,targetBalance,currency}' = 'JPY' THEN
    canonical_command #>> '{body,targetBalance,amount}' ~ '^-?(0|[1-9][0-9]*)$'
    ELSE canonical_command #>> '{body,targetBalance,amount}' ~ '^-?(0|[1-9][0-9]*)[.][0-9]{2}$' END
AND canonical_command #>> '{body,targetBalance,amount}' NOT IN ('-0', '-0.00')
AND char_length(split_part(
    ltrim(canonical_command #>> '{body,targetBalance,amount}', '-'), '.', 1)) <= 131072
), FALSE))) OR (COALESCE(command_version = '1'
AND canonical_command = jsonb_build_object('commandVersion', command_version,
 'operation', 'createFinanceAdvance', 'targetLedgerId', canonical_command->'targetLedgerId',
 'body', canonical_command->'body')
AND jsonb_typeof(canonical_command->'targetLedgerId') = 'string'
AND canonical_command->>'targetLedgerId' ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
AND canonical_command->'body' = jsonb_build_object(
 'fundingAccountId', canonical_command #> '{body,fundingAccountId}',
 'participantId', canonical_command #> '{body,participantId}',
 'transactionDate', canonical_command #> '{body,transactionDate}',
 'amount', canonical_command #> '{body,amount}',
 'ownerShare', canonical_command #> '{body,ownerShare}',
 'participantShare', canonical_command #> '{body,participantShare}',
 'note', canonical_command #> '{body,note}',
 'previewToken', canonical_command #> '{body,previewToken}')
AND jsonb_typeof(canonical_command #> '{body,fundingAccountId}') = 'string'
AND canonical_command #>> '{body,fundingAccountId}' ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
AND jsonb_typeof(canonical_command #> '{body,participantId}') = 'string'
AND canonical_command #>> '{body,participantId}' ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
AND jsonb_typeof(canonical_command #> '{body,previewToken}') = 'string'
AND canonical_command #>> '{body,previewToken}' ~ '^[0-9a-f]{64}$'
AND jsonb_typeof(canonical_command #> '{body,transactionDate}') = 'string'
AND canonical_command #>> '{body,transactionDate}' ~
    '^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$'
AND (canonical_command #>> '{body,transactionDate}' )::date BETWEEN DATE '0001-01-01' AND DATE
    '9999-12-31'
AND (canonical_command #> '{body,note}' = 'null'::jsonb OR
 (jsonb_typeof(canonical_command #> '{body,note}') = 'string'
 AND char_length(canonical_command #>> '{body,note}') BETWEEN 1 AND 500
AND canonical_command #>> '{body,note}' = regexp_replace(canonical_command #>> '{body,note}' ,
    '^[[:space:]]+|[[:space:]]+$' , '' , 'g' )))
AND canonical_command #> '{body,amount}' = jsonb_build_object('amount', canonical_command #>
    '{body,amount,amount}' , 'currency' , canonical_command #> '{body,amount,currency}' )
AND jsonb_typeof(canonical_command #> '{body,amount,amount}') = 'string'
AND canonical_command #>> '{body,amount,currency}' IN ('CNY', 'USD', 'JPY')
AND CASE WHEN canonical_command #>> '{body,amount,currency}' = 'JPY' THEN
 canonical_command #>> '{body,amount,amount}' ~ '^(0|[1-9][0-9]*)$'
 ELSE canonical_command #>> '{body,amount,amount}' ~ '^(0|[1-9][0-9]*)[.][0-9]{2}$' END
AND canonical_command #>> '{body,amount,amount}' NOT IN ('0', '0.00')
AND char_length(split_part(canonical_command #>> '{body,amount,amount}', '.', 1)) <= 131072
AND canonical_command #> '{body,participantShare}' = canonical_command #> '{body,amount}'
AND canonical_command #> '{body,ownerShare}' = jsonb_build_object('currency', canonical_command
    #> '{body,amount,currency}' , 'amount' , CASE WHEN canonical_command #>>
    '{body,amount,currency}' = 'JPY' THEN '0' ELSE '0.00' END)
, FALSE))
) OR
(command_version = '2' AND finance_advance_v2_command_valid(canonical_command))) OR
(command_version = '1' AND finance_settlement_v1_command_valid(canonical_command)) OR
(command_version = '2' AND finance_settlement_v2_command_valid(canonical_command))) OR
(command_version = '3' AND finance_settlement_v3_command_valid(canonical_command))) OR
(command_version = '4' AND finance_settlement_v4_command_valid(canonical_command))
) OR (command_version = '5' AND finance_settlement_v5_command_valid(canonical_command))
) OR (command_version = '6' AND finance_settlement_v6_command_valid(canonical_command))
"""
NEW_COMMAND = (
    f"({OLD_COMMAND}) OR "
    "(command_version = '7' AND finance_settlement_v7_command_valid(canonical_command))"
)
OLD_ACTION = """
(receiving_account_id IS NOT NULL AND reason IS NULL) OR
(reason IS NOT NULL AND reason = btrim(reason) AND char_length(reason) BETWEEN 1 AND 500
AND selected_obligation_ids IS NOT NULL)
"""
NEW_ACTION = (
    "(action_kind = 'creditApplication' AND receiving_account_id IS NULL "
    "AND reason IS NULL AND selected_obligation_ids IS NOT NULL) OR "
    f"(action_kind = 'standard' AND ({OLD_ACTION}))"
)
VALIDATOR = r"""
CREATE FUNCTION finance_settlement_v7_command_valid(command jsonb) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE AS $function$
DECLARE validation_command jsonb;
BEGIN
    IF command->>'commandVersion' IS DISTINCT FROM '7'
       OR command #>> '{body,kind}' IS DISTINCT FROM 'creditApplication'
       OR (command->'body') ? 'reason'
    THEN RETURN FALSE; END IF;
    -- Validate the shared closed amount/selection/date grammar only. The
    -- synthetic reason is never admitted or executed as a forgiveness action.
    validation_command := jsonb_set(jsonb_set(command, '{commandVersion}', '"5"'::jsonb),
        '{body}', (command->'body') || jsonb_build_object(
            'kind', 'forgiveness', 'reason', 'credit application validation'));
    RETURN COALESCE(finance_settlement_v5_command_valid(validation_command), FALSE);
EXCEPTION WHEN data_exception THEN RETURN FALSE;
END;
$function$;
"""


def upgrade() -> None:
    op.execute(VALIDATOR)
    op.drop_constraint("ck_finance_submissions_command", "finance_submissions", type_="check")
    op.create_check_constraint("ck_finance_submissions_command", "finance_submissions", NEW_COMMAND)
    op.add_column(
        "finance_settlements",
        sa.Column("action_kind", sa.Text(), nullable=False, server_default="standard"),
    )
    op.drop_constraint("ck_finance_settlements_action", "finance_settlements", type_="check")
    op.create_check_constraint("ck_finance_settlements_action", "finance_settlements", NEW_ACTION)
    op.create_unique_constraint(
        "uq_finance_credit_origin_ledger", "finance_credit_origins", ["settlement_id", "ledger_id"]
    )
    op.create_table(
        "finance_credit_application_allocations",
        sa.Column("settlement_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column("ledger_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("obligation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("origin_settlement_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount", sa.Numeric(), nullable=False),
        sa.Column("remaining_after", sa.Numeric(), nullable=False),
        sa.Column("origin_available_before", sa.Numeric(), nullable=False),
        sa.UniqueConstraint("transaction_id", name="uq_finance_credit_application_transaction"),
        sa.UniqueConstraint(
            "settlement_id",
            "obligation_id",
            "origin_settlement_id",
            name="uq_finance_credit_application_fragment",
        ),
        sa.ForeignKeyConstraint(
            ["settlement_id", "ledger_id"],
            ["finance_settlements.id", "finance_settlements.ledger_id"],
        ),
        sa.ForeignKeyConstraint(
            ["obligation_id", "ledger_id"],
            ["finance_obligations.id", "finance_obligations.ledger_id"],
        ),
        sa.ForeignKeyConstraint(
            ["origin_settlement_id", "ledger_id"],
            ["finance_credit_origins.settlement_id", "finance_credit_origins.ledger_id"],
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id", "ledger_id"],
            ["finance_transactions.id", "finance_transactions.ledger_id"],
        ),
        sa.CheckConstraint("position >= 1", name="ck_finance_credit_application_position"),
        sa.CheckConstraint(
            "amount > 0 AND amount NOT IN "
            "('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name="ck_finance_credit_application_amount",
        ),
        sa.CheckConstraint(
            "remaining_after >= 0 AND remaining_after NOT IN "
            "('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name="ck_finance_credit_application_remaining",
        ),
        sa.CheckConstraint(
            "origin_available_before >= amount AND origin_available_before NOT IN "
            "('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name="ck_finance_credit_application_available",
        ),
    )


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM finance_submissions WHERE command_version = '7')
           OR EXISTS (SELECT 1 FROM finance_settlements WHERE action_kind = 'creditApplication')
           OR EXISTS (SELECT 1 FROM finance_credit_application_allocations)
        THEN RAISE EXCEPTION 'Cannot downgrade with credit application evidence';
        END IF;
    END $$""")
    op.drop_table("finance_credit_application_allocations")
    op.drop_constraint("uq_finance_credit_origin_ledger", "finance_credit_origins", type_="unique")
    op.drop_constraint("ck_finance_settlements_action", "finance_settlements", type_="check")
    op.create_check_constraint("ck_finance_settlements_action", "finance_settlements", OLD_ACTION)
    op.drop_column("finance_settlements", "action_kind")
    op.drop_constraint("ck_finance_submissions_command", "finance_submissions", type_="check")
    op.create_check_constraint("ck_finance_submissions_command", "finance_submissions", OLD_COMMAND)
    op.execute("DROP FUNCTION finance_settlement_v7_command_valid(jsonb)")
