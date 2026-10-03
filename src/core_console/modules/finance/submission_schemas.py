"""Closed Ledger-specific submission evidence; no resource snapshots."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel

from core_console.problems import ProblemDetails


class _Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class LedgerCreatedResource(_Evidence):
    type: Literal["ledger"]
    id: UUID


class LedgerCreatedOutcome(_Evidence):
    kind: Literal["created"]
    resource: LedgerCreatedResource


class LedgerRejectionProblem(_Evidence):
    type: str
    title: str
    status: Literal[409]
    code: Literal["finance_ledger_name_conflict"]
    detail: str


class LedgerRejectedOutcome(_Evidence):
    kind: Literal["rejected"]
    problem: LedgerRejectionProblem


class _LedgerReceipt(_Evidence):
    submission_id: UUID = Field(alias="submissionId")
    command_version: Literal["1"] = Field(alias="commandVersion")
    operation: Literal["createFinanceLedger"]
    target_ledger_id: None = Field(alias="targetLedgerId")
    admitted_at: datetime = Field(alias="admittedAt")
    resolved_at: datetime = Field(alias="resolvedAt")


class LedgerCreatedReceipt(_LedgerReceipt):
    outcome: LedgerCreatedOutcome


class LedgerRejectedReceipt(_LedgerReceipt):
    outcome: LedgerRejectedOutcome


class LedgerSubmissionReceipt(_LedgerReceipt):
    outcome: Annotated[LedgerCreatedOutcome | LedgerRejectedOutcome, Field(discriminator="kind")]


class LedgerSubmissionUnfinished(_Evidence):
    state: Literal["unfinished"]
    submission_id: UUID = Field(alias="submissionId")
    command_version: Literal["1"] = Field(alias="commandVersion")
    operation: Literal["createFinanceLedger"]
    target_ledger_id: None = Field(alias="targetLedgerId")
    admitted_at: datetime = Field(alias="admittedAt")


class LedgerSubmissionTerminal(_Evidence):
    state: Literal["terminal"]
    receipt: LedgerSubmissionReceipt


class FinanceSubmissionResponse(
    RootModel[
        Annotated[
            LedgerSubmissionUnfinished | LedgerSubmissionTerminal, Field(discriminator="state")
        ]
    ]
):
    """Known-ID lookup, deliberately excluding canonical content."""


class LedgerCommandValidationRejection(_Evidence):
    kind: Literal["definitivelyNotAdmitted"]
    submission_id: UUID = Field(alias="submissionId")
    command_version: Literal["1"] = Field(alias="commandVersion")
    owner_id: UUID = Field(alias="ownerId")
    operation: Literal["createFinanceLedger"]
    target_ledger_id: None = Field(alias="targetLedgerId")
    attempted_body: dict[str, JsonValue] = Field(alias="attemptedBody")


class LedgerTerminalProblem(ProblemDetails):
    model_config = ConfigDict(extra="forbid")
    status: Literal[409]
    code: Literal["finance_ledger_name_conflict"]
    submissionReceipt: LedgerRejectedReceipt


class LedgerValidationProblem(ProblemDetails):
    model_config = ConfigDict(extra="forbid")
    status: Literal[422]
    code: Literal["validation_error"]
    errors: list[dict[str, JsonValue]]
    commandValidationRejection: LedgerCommandValidationRejection


class SubmissionNonterminalProblem(ProblemDetails):
    """An unresolved response cannot masquerade as typed terminal evidence."""

    model_config = ConfigDict(extra="forbid")
    errors: list[dict[str, JsonValue]] | None = None


class LedgerConflictResponse(RootModel[SubmissionNonterminalProblem | LedgerTerminalProblem]):
    """Nonterminal protocol conflict or terminal business rejection."""


class LedgerValidationResponse(RootModel[SubmissionNonterminalProblem | LedgerValidationProblem]):
    """Generic validation/version failure or correlated Q29 evidence."""
