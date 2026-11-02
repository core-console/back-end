"""Retain cash receipt corrections, effective cash, and durable grouped outcomes.

Revision ID: 20261102_01
Revises: 20261031_01
"""

import sqlalchemy as sa
from alembic import op

revision = "20261102_01"
down_revision = "20261031_01"
branch_labels = None
depends_on = None

OLD_COMMAND = r"""
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

OLD_OUTCOME = r"""
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

NEW_COMMAND = r"""((((((((((
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
    (command_version = '1' AND finance_advance_correction_v1_command_valid(canonical_command)))
    OR (command_version = '1' AND
    finance_settlement_correction_v1_command_valid(canonical_command))"""

NEW_OUTCOME = r"""(((
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
WHEN 'createFinanceAdvance' THEN 'advance' WHEN 'createFinanceSettlement' THEN 'settlement' WHEN
    'createFinanceAdvanceCorrection' THEN 'advanceCorrection' WHEN
    'createFinanceSettlementCorrection' THEN 'settlementCorrection' END,
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
 'finance_advance_correction_ineligible', 'finance_linked_record_protected'), FALSE))) OR
(COALESCE(canonical_command->>'operation' = 'createFinanceSettlementCorrection'
AND retention_ledger_id::text = canonical_command->>'targetLedgerId'
AND resolved_at IS NOT NULL AND resolved_at >= admitted_at
AND jsonb_typeof(terminal_outcome #> '{problem,detail}') = 'string'
AND terminal_outcome = jsonb_build_object('kind', 'rejected', 'problem', jsonb_build_object(
 'type', 'about:blank', 'title', CASE terminal_outcome #>> '{problem,code}'
 WHEN 'validation_error' THEN 'Unprocessable Entity'
 WHEN 'finance_category_not_found' THEN 'Not Found'
 WHEN 'finance_account_not_found' THEN 'Not Found'
 WHEN 'finance_participant_not_found' THEN 'Not Found'
 WHEN 'finance_settlement_not_found' THEN 'Not Found' ELSE 'Conflict' END,
 'status', CASE terminal_outcome #>> '{problem,code}' WHEN 'validation_error' THEN 422
WHEN 'finance_category_not_found' THEN 404 WHEN 'finance_account_not_found' THEN 404 WHEN
    'finance_participant_not_found' THEN 404
 WHEN 'finance_settlement_not_found' THEN 404 ELSE 409 END,
 'code', terminal_outcome #> '{problem,code}', 'detail', terminal_outcome #> '{problem,detail}'))
AND terminal_outcome #>> '{problem,code}' IN ('validation_error', 'finance_category_not_found' ,
    'finance_category_archived' , 'finance_account_not_found' ,
 'finance_participant_not_found', 'finance_settlement_not_found', 'finance_account_archived',
 'finance_participant_archived', 'finance_settlement_correction_preview_stale',
 'finance_settlement_correction_ineligible', 'finance_linked_record_protected'), FALSE))"""

VALIDATOR = r"""
CREATE FUNCTION finance_settlement_correction_v1_command_valid(command jsonb)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE AS $function$
DECLARE
    body jsonb := command->'body';
BEGIN
    IF command IS DISTINCT FROM jsonb_build_object(
        'commandVersion', '1', 'operation', 'createFinanceSettlementCorrection',
        'targetLedgerId', command->'targetLedgerId', 'body', body)
       OR body IS DISTINCT FROM jsonb_build_object(
        'settlementId', body->'settlementId', 'amount', body->'amount',
        'reason', body->'reason', 'previewToken', body->'previewToken',
        'excessDisposition', body->'excessDisposition', 'categoryId', body->'categoryId')
       OR jsonb_typeof(body->'reason') IS DISTINCT FROM 'string'
       OR (char_length(body->>'reason') BETWEEN 1 AND 500) IS NOT TRUE
       OR (body->>'excessDisposition' IN ('none', 'retainCredit', 'acceptIncome')) IS NOT TRUE
       OR (body->'categoryId' = 'null'::jsonb OR (
           body->>'excessDisposition' = 'acceptIncome'
           AND jsonb_typeof(body->'categoryId') = 'string'
           AND body->>'categoryId' ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$')) IS NOT TRUE
    THEN RETURN FALSE; END IF;
    -- Reuse the frozen v1 Money/UUID/token/text validators, not today's resource rules.
    RETURN finance_settlement_v1_command_valid(jsonb_build_object(
        'commandVersion', '1', 'operation', 'createFinanceSettlement',
        'targetLedgerId', command->'targetLedgerId', 'body', jsonb_build_object(
            'participantId', body->'settlementId', 'receivingAccountId', body->'settlementId',
            'transactionDate', '2000-01-01', 'amount', body->'amount',
            'note', body->'reason', 'previewToken', body->'previewToken')));
EXCEPTION WHEN data_exception THEN RETURN FALSE;
END;
$function$;
"""

TABLES = r"""
CREATE TABLE finance_settlement_corrections (
	id UUID NOT NULL,
	ledger_id UUID NOT NULL,
	settlement_id UUID NOT NULL,
	actor_id UUID NOT NULL,
	revision INTEGER NOT NULL,
	plan JSONB NOT NULL,
	confirmed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(settlement_id, ledger_id) REFERENCES finance_settlements (id, ledger_id),
	FOREIGN KEY(ledger_id, actor_id) REFERENCES finance_ledgers (id, owner_id),
	CONSTRAINT uq_finance_settlement_corrections_ledger UNIQUE (id, ledger_id),
	CONSTRAINT uq_finance_settlement_corrections_revision UNIQUE (settlement_id, revision),
	CONSTRAINT ck_finance_settlement_corrections_revision CHECK (revision > 0),
	CONSTRAINT ck_finance_settlement_corrections_plan CHECK (jsonb_typeof(plan) = 'object')
)

;

CREATE TABLE finance_settlement_correction_transactions (
	transaction_id UUID NOT NULL,
	ledger_id UUID NOT NULL,
	correction_id UUID NOT NULL,
	PRIMARY KEY (transaction_id),
	FOREIGN KEY(correction_id, ledger_id)
        REFERENCES finance_settlement_corrections (id, ledger_id),
	FOREIGN KEY(transaction_id, ledger_id) REFERENCES finance_transactions (id, ledger_id)
)

;"""


def upgrade() -> None:
    op.execute(sa.text(VALIDATOR))
    for name, expression in (("command", NEW_COMMAND), ("outcome", NEW_OUTCOME)):
        op.drop_constraint(f"ck_finance_submissions_{name}", "finance_submissions", type_="check")
        op.create_check_constraint(
            f"ck_finance_submissions_{name}", "finance_submissions", expression
        )
    op.execute(sa.text(TABLES))
    op.add_column(
        "finance_settlement_allocations",
        sa.Column("effective_cash_paid", sa.Numeric(), nullable=True),
    )
    op.create_check_constraint(
        "ck_finance_settlement_allocation_effective_cash",
        "finance_settlement_allocations",
        "effective_cash_paid IS NULL OR (kind = 'cash' AND effective_cash_paid >= 0 "
        "AND effective_cash_paid NOT IN "
        "('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric))",
    )
    op.add_column(
        "finance_credit_origins", sa.Column("effective_amount", sa.Numeric(), nullable=True)
    )
    op.create_check_constraint(
        "ck_finance_credit_effective_amount",
        "finance_credit_origins",
        "effective_amount IS NULL OR (effective_amount >= 0 AND effective_amount NOT IN "
        "('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric))",
    )


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM finance_settlement_corrections)
           OR EXISTS (SELECT 1 FROM finance_submissions WHERE canonical_command->>'operation' =
             'createFinanceSettlementCorrection')
OR EXISTS (SELECT 1 FROM finance_settlement_allocations WHERE effective_cash_paid IS NOT NULL)
           OR EXISTS (SELECT 1 FROM finance_credit_origins WHERE effective_amount IS NOT NULL)
        THEN RAISE EXCEPTION 'Cannot downgrade with Settlement correction evidence'; END IF;
    END $$""")
    op.drop_constraint(
        "ck_finance_credit_effective_amount", "finance_credit_origins", type_="check"
    )
    op.drop_column("finance_credit_origins", "effective_amount")
    op.drop_constraint(
        "ck_finance_settlement_allocation_effective_cash",
        "finance_settlement_allocations",
        type_="check",
    )
    op.drop_column("finance_settlement_allocations", "effective_cash_paid")
    op.drop_table("finance_settlement_correction_transactions")
    op.drop_table("finance_settlement_corrections")
    for name, expression in (("command", OLD_COMMAND), ("outcome", OLD_OUTCOME)):
        op.drop_constraint(f"ck_finance_submissions_{name}", "finance_submissions", type_="check")
        op.create_check_constraint(
            f"ck_finance_submissions_{name}", "finance_submissions", expression
        )
    op.execute("DROP FUNCTION finance_settlement_correction_v1_command_valid(jsonb)")
