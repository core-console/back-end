"""Retain forgiveness-aware Settlement Correction commands and effective Allocations."""

from importlib import import_module

import sqlalchemy as sa
from alembic import op

revision = "20261122_01"
down_revision = "20261118_01"
branch_labels = None
depends_on = None

V1_COMMAND = import_module("migrations.versions.20261102_01_settlement_corrections").NEW_COMMAND
V2_COMMAND = (
    f"({V1_COMMAND}) OR (command_version = '2' "
    "AND finance_settlement_correction_v2_command_valid(canonical_command))"
)

VALIDATOR = """
CREATE FUNCTION finance_settlement_correction_v2_command_valid(command jsonb)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE AS $function$
DECLARE
    body jsonb := command->'body';
    cash jsonb := body->'amount';
    forgiven jsonb := body->'forgivenessAmount';
    currency text := cash->>'currency';
    zero jsonb := jsonb_build_object('currency', currency,
        'amount', CASE currency WHEN 'JPY' THEN '0' ELSE '0.00' END);
    positive jsonb := jsonb_build_object('currency', currency,
        'amount', CASE currency WHEN 'JPY' THEN '1' ELSE '1.00' END);
    base jsonb := jsonb_set(command, '{commandVersion}', '"1"');
BEGIN
    IF command->>'commandVersion' IS DISTINCT FROM '2'
       OR jsonb_typeof(command->'commandVersion') IS DISTINCT FROM 'string'
       OR cash IS NULL OR forgiven IS NULL
       OR currency IS DISTINCT FROM forgiven->>'currency'
       OR (cash = zero AND forgiven = zero)
    THEN RETURN FALSE; END IF;
    base := jsonb_set(base, '{body}', body - 'forgivenessAmount');
    RETURN finance_settlement_correction_v1_command_valid(jsonb_set(base, '{body,amount}',
              CASE WHEN cash = zero THEN positive ELSE cash END))
       AND finance_settlement_correction_v1_command_valid(jsonb_set(base, '{body,amount}',
              CASE WHEN forgiven = zero THEN positive ELSE forgiven END));
END;
$function$;
"""

KIND_V1 = (
    "(kind = 'cash' AND cash_paid > 0 AND forgiven = 0) OR "
    "(kind = 'forgiveness' AND cash_paid = 0 AND forgiven > 0 "
    "AND forgiven NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric))"
)
KIND_V2 = KIND_V1.replace(
    "cash_paid > 0", "(cash_paid > 0 OR effective_cash_paid IS NOT NULL)"
).replace("forgiven > 0", "forgiven >= 0 AND (forgiven > 0 OR effective_forgiven IS NOT NULL)")

FORGIVEN_V1 = (
    "effective_forgiven IS NULL OR (kind = 'forgiveness' AND effective_forgiven >= 0 "
    "AND effective_forgiven <= forgiven)"
)
FORGIVEN_V2 = FORGIVEN_V1.replace(
    "effective_forgiven <= forgiven",
    "effective_forgiven NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
)


def _constraints(command: str, kind: str, forgiven: str) -> None:
    for table, name, expression in (
        ("finance_submissions", "ck_finance_submissions_command", command),
        ("finance_settlement_allocations", "ck_finance_settlement_allocation_kind", kind),
        (
            "finance_settlement_allocations",
            "ck_finance_settlement_allocation_effective_forgiven",
            forgiven,
        ),
    ):
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, expression)


def upgrade() -> None:
    op.execute(VALIDATOR)
    _constraints(V2_COMMAND, KIND_V2, FORGIVEN_V2)
    op.add_column(
        "finance_settlements", sa.Column("selected_allocation_order", sa.ARRAY(sa.Uuid()))
    )
    op.add_column("finance_settlements", sa.Column("effective_cash_amount", sa.Numeric()))
    op.create_check_constraint(
        "ck_finance_settlements_effective_cash_amount",
        "finance_settlements",
        "effective_cash_amount IS NULL OR (action_kind = 'standard' AND reason IS NOT NULL "
        "AND receiving_account_id IS NOT NULL AND effective_cash_amount >= 0 "
        "AND effective_cash_amount NOT IN "
        "('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric))",
    )


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM finance_submissions WHERE command_version = '2'
                     AND canonical_command->>'operation' = 'createFinanceSettlementCorrection')
          THEN RAISE EXCEPTION 'Cannot downgrade with forgiveness Correction evidence'; END IF;
        END $$
    """)
    _constraints(V1_COMMAND, KIND_V1, FORGIVEN_V1)
    op.drop_column("finance_settlements", "selected_allocation_order")
    op.drop_column("finance_settlements", "effective_cash_amount")
    op.execute("DROP FUNCTION finance_settlement_correction_v2_command_valid(jsonb)")
