"""Extend Advance composition and retain the frozen version 1 submission grammar."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261018_02"
down_revision = "20261018_01"
branch_labels = None
depends_on = None

OLD_COMMAND = r"""
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
"""

V2_VALIDATOR = r"""
CREATE FUNCTION finance_advance_v2_command_valid(command jsonb) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE AS $function$
DECLARE
    body jsonb := command->'body';
    item jsonb;
    value jsonb;
    currency text := body #>> '{amount,currency}';
    total numeric;
    allocated numeric;
    previous_participant text := '';
BEGIN
    IF command IS DISTINCT FROM jsonb_build_object(
        'commandVersion', '2', 'operation', 'createFinanceAdvance',
        'targetLedgerId', command->'targetLedgerId', 'body', body)
       OR jsonb_typeof(command->'targetLedgerId') IS DISTINCT FROM 'string'
       OR (command->>'targetLedgerId' ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$') IS NOT TRUE
       OR body IS DISTINCT FROM jsonb_build_object(
           'fundingAccountId', body->'fundingAccountId', 'transactionDate', body->'transactionDate',
           'amount', body->'amount', 'ownerShare', body->'ownerShare',
           'participantShares', body->'participantShares', 'note', body->'note',
           'previewToken', body->'previewToken')
       OR jsonb_typeof(body->'fundingAccountId') IS DISTINCT FROM 'string'
       OR (body->>'fundingAccountId' ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$') IS NOT TRUE
       OR jsonb_typeof(body->'previewToken') IS DISTINCT FROM 'string'
       OR (body->>'previewToken' ~ '^[0-9a-f]{64}$') IS NOT TRUE
       OR jsonb_typeof(body->'transactionDate') IS DISTINCT FROM 'string'
       OR (body->>'transactionDate' ~
           '^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$') IS NOT TRUE
       OR ((body->>'transactionDate')::date
           BETWEEN DATE '0001-01-01' AND DATE '9999-12-31') IS NOT TRUE
       OR (body->'note' = 'null'::jsonb OR (
           jsonb_typeof(body->'note') = 'string'
           AND char_length(body->>'note') BETWEEN 1 AND 500
           AND body->>'note' = regexp_replace(
               body->>'note', '^[[:space:]]+|[[:space:]]+$', '', 'g'))) IS NOT TRUE
       OR (currency IN ('CNY', 'USD', 'JPY')) IS NOT TRUE
       OR jsonb_typeof(body->'participantShares') IS DISTINCT FROM 'array'
    THEN RETURN FALSE; END IF;
    IF jsonb_array_length(body->'participantShares') < 1 THEN RETURN FALSE; END IF;
    -- Validate every canonical Money, including total and owner share, before casting.
    FOR value IN SELECT body->'amount' UNION ALL SELECT body->'ownerShare'
                 UNION ALL SELECT share->'amount'
                 FROM jsonb_array_elements(body->'participantShares') share
    LOOP
        IF value IS DISTINCT FROM
           jsonb_build_object('amount', value->'amount', 'currency', currency)
           OR jsonb_typeof(value->'amount') IS DISTINCT FROM 'string'
           OR (CASE WHEN currency = 'JPY' THEN value->>'amount' ~ '^(0|[1-9][0-9]*)$'
                    ELSE value->>'amount' ~ '^(0|[1-9][0-9]*)[.][0-9]{2}$' END) IS NOT TRUE
           OR char_length(split_part(value->>'amount', '.', 1)) > 131072
        THEN RETURN FALSE; END IF;
    END LOOP;
    total := (body #>> '{amount,amount}')::numeric;
    allocated := (body #>> '{ownerShare,amount}')::numeric;
    IF total <= 0 THEN RETURN FALSE; END IF;
    FOR item IN SELECT * FROM jsonb_array_elements(body->'participantShares')
    LOOP
        IF item IS DISTINCT FROM jsonb_build_object(
            'participantId', item->'participantId', 'amount', item->'amount')
           OR jsonb_typeof(item->'participantId') IS DISTINCT FROM 'string'
           OR (item->>'participantId' ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$') IS NOT TRUE
           OR item->>'participantId' COLLATE "C" <= previous_participant COLLATE "C"
           OR (item #>> '{amount,amount}')::numeric <= 0
        THEN RETURN FALSE; END IF;
        previous_participant := item->>'participantId';
        allocated := allocated + (item #>> '{amount,amount}')::numeric;
    END LOOP;
    RETURN allocated = total;
EXCEPTION WHEN data_exception THEN RETURN FALSE;
END;
$function$;
"""

NEW_COMMAND = (
    f"({OLD_COMMAND}) OR "
    "(command_version = '2' AND finance_advance_v2_command_valid(canonical_command))"
)


def upgrade() -> None:
    op.execute(V2_VALIDATOR)
    op.drop_constraint("ck_finance_submissions_command", "finance_submissions", type_="check")
    op.create_check_constraint("ck_finance_submissions_command", "finance_submissions", NEW_COMMAND)
    op.add_column(
        "finance_advances",
        sa.Column("owner_share", sa.Numeric(), nullable=False, server_default="0"),
    )
    op.add_column(
        "finance_advances",
        sa.Column("command_version", sa.Text(), nullable=False, server_default="1"),
    )
    op.create_check_constraint(
        "ck_finance_advances_version", "finance_advances", "command_version IN ('1', '2')"
    )
    op.create_check_constraint(
        "ck_finance_advances_owner_share",
        "finance_advances",
        "owner_share >= 0 AND owner_share NOT IN "
        "('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
    )
    op.drop_constraint("uq_finance_obligations_advance", "finance_obligations", type_="unique")
    op.create_unique_constraint(
        "uq_finance_obligations_participant",
        "finance_obligations",
        ["advance_id", "participant_id"],
    )
    op.create_table(
        "finance_advance_transactions",
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("ledger_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("advance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["advance_id", "ledger_id"], ["finance_advances.id", "finance_advances.ledger_id"]
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id", "ledger_id"],
            ["finance_transactions.id", "finance_transactions.ledger_id"],
        ),
    )


def downgrade() -> None:
    # Retained unfinished/terminal evidence must never be erased or made unreadable.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM finance_submissions WHERE command_version = '2')
           OR EXISTS (SELECT 1 FROM finance_advances WHERE command_version = '2')
        THEN RAISE EXCEPTION 'Cannot downgrade with retained version 2 Advance evidence';
        END IF;
    END $$""")
    op.drop_table("finance_advance_transactions")
    op.drop_constraint("uq_finance_obligations_participant", "finance_obligations", type_="unique")
    op.create_unique_constraint(
        "uq_finance_obligations_advance", "finance_obligations", ["advance_id"]
    )
    op.drop_constraint("ck_finance_advances_owner_share", "finance_advances", type_="check")
    op.drop_constraint("ck_finance_advances_version", "finance_advances", type_="check")
    op.drop_column("finance_advances", "command_version")
    op.drop_column("finance_advances", "owner_share")
    op.drop_constraint("ck_finance_submissions_command", "finance_submissions", type_="check")
    op.create_check_constraint("ck_finance_submissions_command", "finance_submissions", OLD_COMMAND)
    op.execute("DROP FUNCTION finance_advance_v2_command_valid(jsonb)")
