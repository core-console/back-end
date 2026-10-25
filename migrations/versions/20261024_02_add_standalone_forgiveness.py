"""Retain standalone forgiveness and exact non-cash Expense evidence."""

import sqlalchemy as sa
from alembic import op

revision = "20261024_02"
down_revision = "20261024_01"
branch_labels = None
depends_on = None

# Frozen prior grammar: future runtime model edits cannot rewrite this migration.
OLD_COMMAND = r"""
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
"""
NEW_COMMAND = (
    f"({OLD_COMMAND}) OR "
    "(command_version = '5' AND finance_settlement_v5_command_valid(canonical_command))"
)
VALIDATOR = r"""
CREATE FUNCTION finance_settlement_v5_command_valid(command jsonb) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE AS $function$
DECLARE
    reason jsonb := command #> '{body,reason}';
    base jsonb;
BEGIN
    IF command->>'commandVersion' IS DISTINCT FROM '5'
       OR command #>> '{body,kind}' IS DISTINCT FROM 'forgiveness'
       OR jsonb_typeof(reason) IS DISTINCT FROM 'string'
       OR char_length(reason #>> '{}') NOT BETWEEN 1 AND 500
       OR (reason #>> '{}') IS DISTINCT FROM
          regexp_replace(reason #>> '{}', '^[[:space:]]+|[[:space:]]+$', '', 'g')
       OR (command->'body') ? 'receivingAccountId'
    THEN RETURN FALSE; END IF;
    -- Reuse the frozen v4 grammar for exact Money, date, selection and envelope;
    -- the synthetic Account exists only inside validation, never as financial evidence.
    base := jsonb_set(jsonb_set(command, '{commandVersion}', '"4"'::jsonb), '{body}',
        ((command->'body') - 'kind' - 'reason') ||
        '{"receivingAccountId":"00000000-0000-0000-0000-000000000000"}'::jsonb);
    RETURN COALESCE(finance_settlement_v4_command_valid(base)
        AND NOT ((command->'body') ? 'excessDisposition'), FALSE);
EXCEPTION WHEN data_exception THEN RETURN FALSE;
END;
$function$;
"""


def upgrade() -> None:
    op.execute(VALIDATOR)
    op.add_column("finance_settlements", sa.Column("reason", sa.Text(), nullable=True))
    op.alter_column("finance_settlements", "receiving_account_id", nullable=True)
    op.create_check_constraint(
        "ck_finance_settlements_action",
        "finance_settlements",
        "(receiving_account_id IS NOT NULL AND reason IS NULL) OR (receiving_account_id "
        "IS NULL AND reason IS NOT NULL AND reason = btrim(reason) AND "
        "char_length(reason) BETWEEN 1 AND 500 AND selected_obligation_ids IS NOT NULL)",
    )
    op.add_column(
        "finance_settlement_allocations",
        sa.Column("kind", sa.Text(), server_default="cash", nullable=False),
    )
    op.add_column(
        "finance_settlement_allocations",
        sa.Column("forgiven", sa.Numeric(), server_default="0", nullable=False),
    )
    op.drop_constraint(
        "ck_finance_settlement_allocation_cash", "finance_settlement_allocations", type_="check"
    )
    op.create_check_constraint(
        "ck_finance_settlement_allocation_cash",
        "finance_settlement_allocations",
        "cash_paid >= 0 AND cash_paid NOT IN ('NaN'::numeric, 'Infinity'::numeric, "
        "'-Infinity'::numeric)",
    )
    op.create_check_constraint(
        "ck_finance_settlement_allocation_kind",
        "finance_settlement_allocations",
        "(kind = 'cash' AND cash_paid > 0 AND forgiven = 0) OR (kind = 'forgiveness' AND "
        "cash_paid = 0 AND forgiven > 0 AND forgiven NOT IN ('NaN'::numeric, "
        "'Infinity'::numeric, '-Infinity'::numeric))",
    )
    op.drop_constraint("ck_finance_submissions_command", "finance_submissions", type_="check")
    op.create_check_constraint("ck_finance_submissions_command", "finance_submissions", NEW_COMMAND)


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM finance_settlements WHERE reason IS NOT NULL)
           OR EXISTS (SELECT 1 FROM finance_settlement_allocations WHERE kind = 'forgiveness')
           OR EXISTS (SELECT 1 FROM finance_submissions WHERE command_version = '5')
        THEN RAISE EXCEPTION 'Cannot downgrade with forgiveness evidence';
        END IF;
    END $$""")
    op.drop_constraint("ck_finance_submissions_command", "finance_submissions", type_="check")
    op.create_check_constraint("ck_finance_submissions_command", "finance_submissions", OLD_COMMAND)
    op.drop_constraint(
        "ck_finance_settlement_allocation_kind", "finance_settlement_allocations", type_="check"
    )
    op.drop_constraint(
        "ck_finance_settlement_allocation_cash", "finance_settlement_allocations", type_="check"
    )
    op.create_check_constraint(
        "ck_finance_settlement_allocation_cash",
        "finance_settlement_allocations",
        "cash_paid > 0 AND cash_paid NOT IN ('NaN'::numeric, 'Infinity'::numeric, "
        "'-Infinity'::numeric)",
    )
    op.drop_column("finance_settlement_allocations", "forgiven")
    op.drop_column("finance_settlement_allocations", "kind")
    op.drop_constraint("ck_finance_settlements_action", "finance_settlements", type_="check")
    op.alter_column("finance_settlements", "receiving_account_id", nullable=False)
    op.drop_column("finance_settlements", "reason")
    op.execute("DROP FUNCTION finance_settlement_v5_command_valid(jsonb)")
