"""Retain coordinated pre-settlement Advance correction evidence."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261031_01"
down_revision = "20261029_01"
branch_labels = None
depends_on = None

OLD_COMMAND = r"""
((((((((
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
    (command_version = '1' AND finance_settlement_v1_command_valid(canonical_command)) OR
    (command_version = '2' AND finance_settlement_v2_command_valid(canonical_command))) OR
    (command_version = '3' AND finance_settlement_v3_command_valid(canonical_command))) OR
    (command_version = '4' AND finance_settlement_v4_command_valid(canonical_command))) OR
    (command_version = '5' AND finance_settlement_v5_command_valid(canonical_command))) OR
    (command_version = '6' AND finance_settlement_v6_command_valid(canonical_command))) OR
    (command_version = '7' AND finance_settlement_v7_command_valid(canonical_command))) OR
    (command_version = '8' AND finance_settlement_v8_command_valid(canonical_command))
"""

OLD_OUTCOME = r"""
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
 OR
(COALESCE(canonical_command->>'operation' = 'createFinanceSettlement'
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
) OR
(COALESCE(command_version IN ('3', '4')
AND canonical_command->>'operation' = 'createFinanceSettlement'
AND retention_ledger_id::text = canonical_command->>'targetLedgerId'
AND resolved_at IS NOT NULL AND resolved_at >= admitted_at
AND jsonb_typeof(terminal_outcome #> '{problem,detail}') = 'string'
AND terminal_outcome = jsonb_build_object('kind', 'rejected', 'problem', jsonb_build_object(
 'type', 'about:blank', 'title', CASE terminal_outcome #>> '{problem,code}'
 WHEN 'finance_category_not_found' THEN 'Not Found' ELSE 'Conflict' END,
 'status', CASE terminal_outcome #>> '{problem,code}'
 WHEN 'finance_category_not_found' THEN 404 ELSE 409 END,
 'code', terminal_outcome #> '{problem,code}', 'detail', terminal_outcome #> '{problem,detail}'))
AND terminal_outcome #>> '{problem,code}' IN
    ('finance_category_not_found', 'finance_category_archived')
, FALSE))

"""

NEW_COMMAND = r"""
(((((((((
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
    (command_version = '1' AND finance_settlement_v1_command_valid(canonical_command)) OR
    (command_version = '2' AND finance_settlement_v2_command_valid(canonical_command))) OR
    (command_version = '3' AND finance_settlement_v3_command_valid(canonical_command))) OR
    (command_version = '4' AND finance_settlement_v4_command_valid(canonical_command))) OR
    (command_version = '5' AND finance_settlement_v5_command_valid(canonical_command))) OR
    (command_version = '6' AND finance_settlement_v6_command_valid(canonical_command))) OR
    (command_version = '7' AND finance_settlement_v7_command_valid(canonical_command))) OR
    (command_version = '8' AND finance_settlement_v8_command_valid(canonical_command))) OR
    (command_version = '1' AND finance_advance_correction_v1_command_valid(canonical_command))
"""

NEW_OUTCOME = r"""
((
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
    'settlement' WHEN 'createFinanceAdvanceCorrection' THEN 'advanceCorrection' END,
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
 OR
(COALESCE(canonical_command->>'operation' = 'createFinanceSettlement'
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
) OR
(COALESCE(command_version IN ('3', '4')
AND canonical_command->>'operation' = 'createFinanceSettlement'
AND retention_ledger_id::text = canonical_command->>'targetLedgerId'
AND resolved_at IS NOT NULL AND resolved_at >= admitted_at
AND jsonb_typeof(terminal_outcome #> '{problem,detail}') = 'string'
AND terminal_outcome = jsonb_build_object('kind', 'rejected', 'problem', jsonb_build_object(
 'type', 'about:blank', 'title', CASE terminal_outcome #>> '{problem,code}'
 WHEN 'finance_category_not_found' THEN 'Not Found' ELSE 'Conflict' END,
 'status', CASE terminal_outcome #>> '{problem,code}'
 WHEN 'finance_category_not_found' THEN 404 ELSE 409 END,
 'code', terminal_outcome #> '{problem,code}', 'detail', terminal_outcome #> '{problem,detail}'))
AND terminal_outcome #>> '{problem,code}' IN
    ('finance_category_not_found', 'finance_category_archived')
, FALSE))
) OR (COALESCE(canonical_command->>'operation' = 'createFinanceAdvanceCorrection'
AND retention_ledger_id::text = canonical_command->>'targetLedgerId'
AND resolved_at IS NOT NULL AND resolved_at >= admitted_at
AND jsonb_typeof(terminal_outcome #> '{problem,detail}') = 'string'
AND terminal_outcome = jsonb_build_object('kind', 'rejected', 'problem', jsonb_build_object(
 'type', 'about:blank', 'title', CASE terminal_outcome #>> '{problem,code}'
 WHEN 'validation_error' THEN 'Unprocessable Entity'
 WHEN 'finance_account_not_found' THEN 'Not Found'
 WHEN 'finance_participant_not_found' THEN 'Not Found'
 WHEN 'finance_advance_not_found' THEN 'Not Found' ELSE 'Conflict' END,
 'status', CASE terminal_outcome #>> '{problem,code}' WHEN 'validation_error' THEN 422
 WHEN 'finance_account_not_found' THEN 404 WHEN 'finance_participant_not_found' THEN 404
 WHEN 'finance_advance_not_found' THEN 404 ELSE 409 END,
 'code', terminal_outcome #> '{problem,code}', 'detail', terminal_outcome #> '{problem,detail}'))
AND terminal_outcome #>> '{problem,code}' IN ('validation_error', 'finance_account_not_found',
 'finance_participant_not_found', 'finance_advance_not_found', 'finance_account_archived',
 'finance_participant_archived', 'finance_advance_correction_preview_stale',
 'finance_advance_correction_ineligible', 'finance_linked_record_protected'), FALSE))
"""

VALIDATOR = r"""

CREATE FUNCTION finance_advance_correction_v1_command_valid(command jsonb) RETURNS boolean
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
        'commandVersion', '1', 'operation', 'createFinanceAdvanceCorrection',
        'targetLedgerId', command->'targetLedgerId', 'body', body)
       OR jsonb_typeof(command->'targetLedgerId') IS DISTINCT FROM 'string'
       OR (command->>'targetLedgerId' ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$') IS NOT TRUE
       OR body IS DISTINCT FROM jsonb_build_object(
           'fundingAccountId', body->'fundingAccountId', 'transactionDate', body->'transactionDate',
           'amount', body->'amount', 'ownerShare', body->'ownerShare',
           'participantShares', body->'participantShares', 'note', body->'note',
           'previewToken', body->'previewToken', 'advanceId', body->'advanceId', 'reason',
    body->'reason')
       OR jsonb_typeof(body->'advanceId') IS DISTINCT FROM 'string'
       OR (body->>'advanceId' ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$') IS NOT TRUE
       OR jsonb_typeof(body->'reason') IS DISTINCT FROM 'string'
       OR (char_length(body->>'reason') BETWEEN 1 AND 500) IS NOT TRUE
       OR body->>'reason' IS DISTINCT FROM regexp_replace(body->>'reason',
    '^[[:space:]]+|[[:space:]]+$', '', 'g')
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


OLD_INTEGRITY = r"""
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

            END IF;
            RETURN NULL;
        END;
        $$
        """
FIXED_INTEGRITY = OLD_INTEGRITY.replace(
    "NEW.transaction_id IS DISTINCT FROM OLD.transaction_id",
    "(to_jsonb(NEW)->>'transaction_id') IS DISTINCT FROM (to_jsonb(OLD)->>'transaction_id')",
)


def upgrade() -> None:
    # A transaction UPDATE has no transaction_id field; use safe record access.
    op.execute(sa.text(FIXED_INTEGRITY))
    op.execute(VALIDATOR)
    for name, expression in (("command", NEW_COMMAND), ("outcome", NEW_OUTCOME)):
        op.drop_constraint(f"ck_finance_submissions_{name}", "finance_submissions", type_="check")
        op.create_check_constraint(
            f"ck_finance_submissions_{name}", "finance_submissions", expression
        )
    op.create_table(
        "finance_advance_corrections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("ledger_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("advance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("plan", postgresql.JSONB(), nullable=False),
        sa.Column(
            "confirmed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["advance_id", "ledger_id"], ["finance_advances.id", "finance_advances.ledger_id"]
        ),
        sa.ForeignKeyConstraint(
            ["ledger_id", "actor_id"], ["finance_ledgers.id", "finance_ledgers.owner_id"]
        ),
        sa.UniqueConstraint(
            "advance_id", "revision", name="uq_finance_advance_corrections_revision"
        ),
        sa.CheckConstraint("revision > 0", name="ck_finance_advance_corrections_revision"),
        sa.CheckConstraint(
            "jsonb_typeof(plan) = 'object'", name="ck_finance_advance_corrections_plan"
        ),
    )
    op.drop_constraint("ck_finance_obligations_principal", "finance_obligations", type_="check")
    op.create_check_constraint(
        "ck_finance_obligations_principal",
        "finance_obligations",
        "principal >= 0 AND "
        "principal NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
    )


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM finance_advance_corrections)
           OR EXISTS (SELECT 1 FROM finance_submissions WHERE canonical_command->>'operation' =
    'createFinanceAdvanceCorrection')
        THEN RAISE EXCEPTION 'Cannot downgrade with Advance correction evidence'; END IF;
    END $$""")
    op.execute(sa.text(OLD_INTEGRITY))
    op.drop_table("finance_advance_corrections")
    op.drop_constraint("ck_finance_obligations_principal", "finance_obligations", type_="check")
    op.create_check_constraint(
        "ck_finance_obligations_principal",
        "finance_obligations",
        "principal > 0 AND "
        "principal NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
    )
    for name, expression in (("command", OLD_COMMAND), ("outcome", OLD_OUTCOME)):
        op.drop_constraint(f"ck_finance_submissions_{name}", "finance_submissions", type_="check")
        op.create_check_constraint(
            f"ck_finance_submissions_{name}", "finance_submissions", expression
        )
    op.execute("DROP FUNCTION finance_advance_correction_v1_command_valid(jsonb)")
