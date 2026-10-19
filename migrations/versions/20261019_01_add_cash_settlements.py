"""Retain ordered cash repayments and extend durable evidence without changing old commands."""

from alembic import op

revision = "20261019_01"
down_revision = "20261018_02"
branch_labels = None
depends_on = None

OLD_COMMAND = r"""
(
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
) OR (command_version = '2' AND finance_advance_v2_command_valid(canonical_command))
"""

OLD_OUTCOME = r"""
(COALESCE(
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
          WHEN 'createFinanceCategory' THEN 'category'
          WHEN 'createFinanceTransaction' THEN 'transaction'
          WHEN 'createBalanceAdjustment' THEN 'transaction'
          WHEN 'createFinanceAdvance' THEN 'advance' END,
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
      OR (canonical_command->>'operation' IN ('createFinanceAccount', 'createFinanceTransaction',
          'createBalanceAdjustment')
        AND jsonb_typeof(terminal_outcome #> '{problem,detail}') = 'string'
        AND terminal_outcome = jsonb_build_object('kind', 'rejected', 'problem',
          jsonb_build_object('type', 'about:blank', 'title', 'Unprocessable Entity', 'status', 422,
            'code', 'validation_error', 'detail', terminal_outcome #> '{problem,detail}')))
      OR (canonical_command->>'operation' IN ('createFinanceTransaction', 'createBalanceAdjustment')
        AND jsonb_typeof(terminal_outcome #> '{problem,detail}') = 'string'
        AND terminal_outcome = jsonb_build_object('kind', 'rejected', 'problem',
          jsonb_build_object('type', 'about:blank',
            'title', CASE WHEN terminal_outcome #>> '{problem,code}' IN
              ('finance_account_archived', 'finance_category_archived',
               'account_balance_changed', 'finance_account_semantics_changed') THEN 'Conflict'
                ELSE 'Not Found' END,
            'status', CASE WHEN terminal_outcome #>> '{problem,code}' IN
              ('finance_account_archived', 'finance_category_archived',
               'account_balance_changed', 'finance_account_semantics_changed')
                THEN 409 ELSE 404 END,
            'code', terminal_outcome #> '{problem,code}', 'detail', terminal_outcome #>
                '{problem,detail}'))
        AND ((canonical_command->>'operation' = 'createFinanceTransaction'
          AND terminal_outcome #>> '{problem,code}' IN (
            'finance_account_archived', 'finance_category_archived',
            'finance_account_not_found', 'finance_category_not_found'))
          OR (canonical_command->>'operation' = 'createBalanceAdjustment'
          AND terminal_outcome #>> '{problem,code}' IN (
            'finance_account_archived', 'finance_account_not_found',
            'account_balance_changed', 'finance_account_semantics_changed'))))
      OR (canonical_command->>'operation' = 'createBalanceAdjustment'
        AND terminal_outcome = jsonb_build_object('kind', 'noChange'))
    ))
), FALSE)) OR (COALESCE(canonical_command->>'operation' = 'createFinanceAdvance'
AND retention_ledger_id::text = canonical_command->>'targetLedgerId'
AND resolved_at IS NOT NULL AND resolved_at >= admitted_at
AND jsonb_typeof(terminal_outcome #> '{problem,detail}') = 'string'
AND terminal_outcome = jsonb_build_object('kind', 'rejected', 'problem', jsonb_build_object(
 'type', 'about:blank', 'title', CASE terminal_outcome #>> '{problem,code}'
 WHEN 'validation_error' THEN 'Unprocessable Entity'
 WHEN 'finance_account_not_found' THEN 'Not Found'
 WHEN 'finance_participant_not_found' THEN 'Not Found' ELSE 'Conflict' END,
 'status', CASE terminal_outcome #>> '{problem,code}' WHEN 'validation_error' THEN 422
WHEN 'finance_account_not_found' THEN 404 WHEN 'finance_participant_not_found' THEN 404 ELSE 409
    END,
 'code', terminal_outcome #> '{problem,code}', 'detail', terminal_outcome #> '{problem,detail}'))
AND terminal_outcome #>> '{problem,code}' IN ('validation_error', 'finance_account_not_found' ,
    'finance_participant_not_found' , 'finance_account_archived' ,
    'finance_participant_archived' , 'finance_advance_preview_stale' ,
    'finance_linked_record_protected' )
, FALSE)) OR (COALESCE(canonical_command->>'operation' IN ('createFinanceTransaction',
    'createBalanceAdjustment' )
AND retention_ledger_id::text = canonical_command->>'targetLedgerId'
AND resolved_at IS NOT NULL AND resolved_at >= admitted_at
AND jsonb_typeof(terminal_outcome #> '{problem,detail}') = 'string'
AND terminal_outcome = jsonb_build_object('kind', 'rejected' , 'problem' ,
    jsonb_build_object('type', 'about:blank' , 'title' , 'Conflict' , 'status' , 409, 'code' ,
    'finance_linked_record_protected' , 'detail' , terminal_outcome #> '{problem,detail}' ))
, FALSE))
"""

NEW_COMMAND = r"""
((
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
) OR (command_version = '2' AND finance_advance_v2_command_valid(canonical_command))) OR
    (command_version = '1' AND finance_settlement_v1_command_valid(canonical_command))
"""

NEW_OUTCOME = r"""
(
(COALESCE(
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
          WHEN 'createFinanceCategory' THEN 'category'
          WHEN 'createFinanceTransaction' THEN 'transaction'
          WHEN 'createBalanceAdjustment' THEN 'transaction'
          WHEN 'createFinanceAdvance' THEN 'advance' WHEN 'createFinanceSettlement' THEN
    'settlement' END,
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
      OR (canonical_command->>'operation' IN ('createFinanceAccount', 'createFinanceTransaction',
          'createBalanceAdjustment')
        AND jsonb_typeof(terminal_outcome #> '{problem,detail}') = 'string'
        AND terminal_outcome = jsonb_build_object('kind', 'rejected', 'problem',
          jsonb_build_object('type', 'about:blank', 'title', 'Unprocessable Entity', 'status', 422,
            'code', 'validation_error', 'detail', terminal_outcome #> '{problem,detail}')))
      OR (canonical_command->>'operation' IN ('createFinanceTransaction', 'createBalanceAdjustment')
        AND jsonb_typeof(terminal_outcome #> '{problem,detail}') = 'string'
        AND terminal_outcome = jsonb_build_object('kind', 'rejected', 'problem',
          jsonb_build_object('type', 'about:blank',
            'title', CASE WHEN terminal_outcome #>> '{problem,code}' IN
              ('finance_account_archived', 'finance_category_archived',
               'account_balance_changed', 'finance_account_semantics_changed') THEN 'Conflict'
                ELSE 'Not Found' END,
            'status', CASE WHEN terminal_outcome #>> '{problem,code}' IN
              ('finance_account_archived', 'finance_category_archived',
               'account_balance_changed', 'finance_account_semantics_changed')
                THEN 409 ELSE 404 END,
            'code', terminal_outcome #> '{problem,code}', 'detail', terminal_outcome #>
                '{problem,detail}'))
        AND ((canonical_command->>'operation' = 'createFinanceTransaction'
          AND terminal_outcome #>> '{problem,code}' IN (
            'finance_account_archived', 'finance_category_archived',
            'finance_account_not_found', 'finance_category_not_found'))
          OR (canonical_command->>'operation' = 'createBalanceAdjustment'
          AND terminal_outcome #>> '{problem,code}' IN (
            'finance_account_archived', 'finance_account_not_found',
            'account_balance_changed', 'finance_account_semantics_changed'))))
      OR (canonical_command->>'operation' = 'createBalanceAdjustment'
        AND terminal_outcome = jsonb_build_object('kind', 'noChange'))
    ))
), FALSE)) OR (COALESCE(canonical_command->>'operation' = 'createFinanceAdvance'
AND retention_ledger_id::text = canonical_command->>'targetLedgerId'
AND resolved_at IS NOT NULL AND resolved_at >= admitted_at
AND jsonb_typeof(terminal_outcome #> '{problem,detail}') = 'string'
AND terminal_outcome = jsonb_build_object('kind', 'rejected', 'problem', jsonb_build_object(
 'type', 'about:blank', 'title', CASE terminal_outcome #>> '{problem,code}'
 WHEN 'validation_error' THEN 'Unprocessable Entity'
 WHEN 'finance_account_not_found' THEN 'Not Found'
 WHEN 'finance_participant_not_found' THEN 'Not Found' ELSE 'Conflict' END,
 'status', CASE terminal_outcome #>> '{problem,code}' WHEN 'validation_error' THEN 422
WHEN 'finance_account_not_found' THEN 404 WHEN 'finance_participant_not_found' THEN 404 ELSE 409
    END,
 'code', terminal_outcome #> '{problem,code}', 'detail', terminal_outcome #> '{problem,detail}'))
AND terminal_outcome #>> '{problem,code}' IN ('validation_error', 'finance_account_not_found' ,
    'finance_participant_not_found' , 'finance_account_archived' ,
    'finance_participant_archived' , 'finance_advance_preview_stale' ,
    'finance_linked_record_protected' )
, FALSE)) OR (COALESCE(canonical_command->>'operation' IN ('createFinanceTransaction',
    'createBalanceAdjustment' )
AND retention_ledger_id::text = canonical_command->>'targetLedgerId'
AND resolved_at IS NOT NULL AND resolved_at >= admitted_at
AND jsonb_typeof(terminal_outcome #> '{problem,detail}') = 'string'
AND terminal_outcome = jsonb_build_object('kind', 'rejected' , 'problem' ,
    jsonb_build_object('type', 'about:blank' , 'title' , 'Conflict' , 'status' , 409, 'code' ,
    'finance_linked_record_protected' , 'detail' , terminal_outcome #> '{problem,detail}' ))
, FALSE))
) OR (COALESCE(canonical_command->>'operation' = 'createFinanceSettlement'
AND retention_ledger_id::text = canonical_command->>'targetLedgerId'
AND resolved_at IS NOT NULL AND resolved_at >= admitted_at
AND jsonb_typeof(terminal_outcome #> '{problem,detail}') = 'string'
AND terminal_outcome = jsonb_build_object('kind', 'rejected', 'problem', jsonb_build_object(
 'type', 'about:blank', 'title', CASE terminal_outcome #>> '{problem,code}'
 WHEN 'validation_error' THEN 'Unprocessable Entity'
 WHEN 'finance_account_not_found' THEN 'Not Found'
 WHEN 'finance_participant_not_found' THEN 'Not Found' ELSE 'Conflict' END,
 'status', CASE terminal_outcome #>> '{problem,code}' WHEN 'validation_error' THEN 422
WHEN 'finance_account_not_found' THEN 404 WHEN 'finance_participant_not_found' THEN 404 ELSE 409
    END,
 'code', terminal_outcome #> '{problem,code}', 'detail', terminal_outcome #> '{problem,detail}'))
AND terminal_outcome #>> '{problem,code}' IN ('validation_error', 'finance_account_not_found' ,
    'finance_participant_not_found' , 'finance_account_archived' ,
    'finance_settlement_preview_stale' ,
    'finance_linked_record_protected' )
, FALSE))
"""

VALIDATOR = r"""
CREATE FUNCTION finance_settlement_v1_command_valid(command jsonb)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE AS $function$
DECLARE
    body jsonb := command->'body';
    value jsonb := body->'amount';
    currency text := value->>'currency';
    field text;
BEGIN
    IF command IS DISTINCT FROM jsonb_build_object(
        'commandVersion', '1', 'operation', 'createFinanceSettlement',
        'targetLedgerId', command->'targetLedgerId', 'body', body)
       OR jsonb_typeof(command->'targetLedgerId') IS DISTINCT FROM 'string'
       OR (command->>'targetLedgerId' ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$') IS NOT TRUE
       OR body IS DISTINCT FROM jsonb_build_object(
         'participantId', body->'participantId', 'receivingAccountId', body->'receivingAccountId',
         'transactionDate', body->'transactionDate', 'amount', value,
         'note', body->'note', 'previewToken', body->'previewToken')
    THEN RETURN FALSE; END IF;
    FOREACH field IN ARRAY ARRAY['participantId', 'receivingAccountId'] LOOP
        IF jsonb_typeof(body->field) IS DISTINCT FROM 'string'
           OR (body->>field ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$') IS NOT TRUE
        THEN RETURN FALSE; END IF;
    END LOOP;
    IF jsonb_typeof(body->'transactionDate') IS DISTINCT FROM 'string'
       OR (body->>'transactionDate' ~ '^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$') IS
    NOT TRUE
       OR (body->>'transactionDate')::date NOT BETWEEN DATE '0001-01-01' AND DATE '9999-12-31'
       OR jsonb_typeof(body->'previewToken') IS DISTINCT FROM 'string'
       OR (body->>'previewToken' ~ '^[0-9a-f]{64}$') IS NOT TRUE
       OR body->'note' IS NULL
       OR (body->'note' <> 'null'::jsonb AND (
         jsonb_typeof(body->'note') IS DISTINCT FROM 'string'
         OR char_length(body->>'note') NOT BETWEEN 1 AND 500
         OR body->>'note' <> regexp_replace(body->>'note', '^[[:space:]]+|[[:space:]]+$', '', 'g')))
       OR value IS DISTINCT FROM jsonb_build_object('amount', value->'amount', 'currency',
    value->'currency')
       OR currency IS NULL OR currency NOT IN ('CNY', 'USD', 'JPY')
       OR jsonb_typeof(value->'amount') IS DISTINCT FROM 'string'
       OR (CASE WHEN currency = 'JPY' THEN value->>'amount' ~ '^(0|[1-9][0-9]*)$'
           ELSE value->>'amount' ~ '^(0|[1-9][0-9]*)[.][0-9]{2}$' END) IS NOT TRUE
       OR char_length(split_part(value->>'amount', '.', 1)) > 131072
       OR (value->>'amount')::numeric <= 0
    THEN RETURN FALSE; END IF;
    RETURN TRUE;
EXCEPTION WHEN data_exception THEN RETURN FALSE;
END;
$function$;
"""

SETTLEMENT_TABLE = r"""
CREATE TABLE finance_settlements (
	id UUID NOT NULL,
	ledger_id UUID NOT NULL,
	participant_id UUID NOT NULL,
	receiving_account_id UUID NOT NULL,
	transaction_date DATE NOT NULL,
	amount NUMERIC NOT NULL,
	currency TEXT NOT NULL,
	note TEXT,
	confirmation_order BIGINT GENERATED BY DEFAULT AS IDENTITY,
	confirmed_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_finance_settlements_id_ledger UNIQUE (id, ledger_id),
	CONSTRAINT uq_finance_settlements_order UNIQUE (confirmation_order),
	FOREIGN KEY(ledger_id) REFERENCES finance_ledgers (id),
	FOREIGN KEY(participant_id, ledger_id) REFERENCES finance_participants (id, ledger_id),
	FOREIGN KEY(receiving_account_id, ledger_id) REFERENCES finance_accounts (id, ledger_id),
	CONSTRAINT ck_finance_settlements_amount CHECK (amount > 0 AND amount NOT IN ('NaN'::numeric,
    'Infinity'::numeric, '-Infinity'::numeric)),
	CONSTRAINT ck_finance_settlements_currency CHECK (currency IN ('CNY', 'USD', 'JPY')),
	CONSTRAINT ck_finance_settlements_scale CHECK ((currency = 'JPY' AND scale(amount) = 0) OR
    (currency IN ('CNY', 'USD') AND scale(amount) <= 2)),
	CONSTRAINT ck_finance_settlements_note CHECK (note IS NULL OR (note = btrim(note) AND
    char_length(note) BETWEEN 1 AND 500))
)
"""

ALLOCATION_TABLE = r"""
CREATE TABLE finance_settlement_allocations (
	settlement_id UUID NOT NULL,
	position INTEGER NOT NULL,
	ledger_id UUID NOT NULL,
	obligation_id UUID NOT NULL,
	transaction_id UUID NOT NULL,
	cash_paid NUMERIC NOT NULL,
	remaining_after NUMERIC NOT NULL,
	PRIMARY KEY (settlement_id, position),
	CONSTRAINT uq_finance_settlement_obligation UNIQUE (settlement_id, obligation_id),
	CONSTRAINT uq_finance_settlement_transaction UNIQUE (transaction_id),
	FOREIGN KEY(settlement_id, ledger_id) REFERENCES finance_settlements (id, ledger_id),
	FOREIGN KEY(obligation_id, ledger_id) REFERENCES finance_obligations (id, ledger_id),
	FOREIGN KEY(transaction_id, ledger_id) REFERENCES finance_transactions (id, ledger_id),
	CONSTRAINT ck_finance_settlement_allocation_position CHECK (position >= 1),
	CONSTRAINT ck_finance_settlement_allocation_cash CHECK (cash_paid > 0 AND cash_paid NOT IN
    ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)),
	CONSTRAINT ck_finance_settlement_allocation_remaining CHECK (remaining_after >= 0 AND
    remaining_after NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric))
)
"""


def upgrade() -> None:
    op.execute(VALIDATOR)
    op.create_unique_constraint(
        "uq_finance_obligations_id_ledger", "finance_obligations", ["id", "ledger_id"]
    )
    op.execute(SETTLEMENT_TABLE)
    op.execute(ALLOCATION_TABLE)
    for name, value in (("command", NEW_COMMAND), ("outcome", NEW_OUTCOME)):
        op.drop_constraint(f"ck_finance_submissions_{name}", "finance_submissions", type_="check")
        op.create_check_constraint(f"ck_finance_submissions_{name}", "finance_submissions", value)


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM finance_settlements)
           OR EXISTS (SELECT 1 FROM finance_submissions
                      WHERE canonical_command->>'operation' = 'createFinanceSettlement')
        THEN RAISE EXCEPTION 'Cannot downgrade with retained Settlement evidence';
        END IF;
    END $$""")
    for name, value in (("command", OLD_COMMAND), ("outcome", OLD_OUTCOME)):
        op.drop_constraint(f"ck_finance_submissions_{name}", "finance_submissions", type_="check")
        op.create_check_constraint(f"ck_finance_submissions_{name}", "finance_submissions", value)
    op.drop_table("finance_settlement_allocations")
    op.drop_table("finance_settlements")
    op.drop_constraint("uq_finance_obligations_id_ledger", "finance_obligations", type_="unique")
    op.execute("DROP FUNCTION finance_settlement_v1_command_valid(jsonb)")
