"""Durable Ledger command bindings, independent of resource projections."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core_console.database.base import Base

# This first slice intentionally admits only Ledger v1 commands. Later slices
# must extend the schema and operation-specific unions together.
COMMAND_CHECK = """
command_version = '1'
AND canonical_command = jsonb_build_object(
    'commandVersion', command_version, 'operation', 'createFinanceLedger',
    'targetLedgerId', NULL, 'body', jsonb_build_object('name', canonical_command #> '{body,name}'))
AND jsonb_typeof(canonical_command #> '{body,name}') = 'string'
AND char_length(canonical_command #>> '{body,name}') BETWEEN 1 AND 100
AND canonical_command #>> '{body,name}' = regexp_replace(
    canonical_command #>> '{body,name}', '^[[:space:]]+|[[:space:]]+$', '', 'g')
"""
OUTCOME_CHECK = """
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


class FinanceSubmission(Base):
    """Exactly unfinished or terminal, unique across one user's commands."""

    __tablename__ = "finance_submissions"
    __table_args__ = (
        CheckConstraint(COMMAND_CHECK, name="ck_finance_submissions_command"),
        CheckConstraint(OUTCOME_CHECK, name="ck_finance_submissions_outcome"),
        ForeignKeyConstraint(
            ["retention_ledger_id", "local_user_id"],
            ["finance_ledgers.id", "finance_ledgers.owner_id"],
            name="fk_finance_submissions_retention_owner",
        ),
    )

    local_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", name="fk_finance_submissions_user"), primary_key=True
    )
    submission_id: Mapped[UUID] = mapped_column(primary_key=True)
    command_version: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_command: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    retention_ledger_id: Mapped[UUID | None]
    admitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    terminal_outcome: Mapped[dict[str, object] | None] = mapped_column(JSONB(none_as_null=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
