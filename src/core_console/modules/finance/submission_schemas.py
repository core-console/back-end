"""Closed operation-specific submission evidence; no resource snapshots."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel

from core_console.problems import ProblemDetails


class _Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class _ReceiptMetadata(_Evidence):
    submission_id: UUID = Field(alias="submissionId")
    admitted_at: datetime = Field(alias="admittedAt")
    resolved_at: datetime = Field(alias="resolvedAt")


class _Receipt(_ReceiptMetadata):
    command_version: Literal["1"] = Field(alias="commandVersion")


class _UnfinishedMetadata(_Evidence):
    state: Literal["unfinished"]
    submission_id: UUID = Field(alias="submissionId")
    admitted_at: datetime = Field(alias="admittedAt")


class _Unfinished(_UnfinishedMetadata):
    command_version: Literal["1"] = Field(alias="commandVersion")


class _CommandValidationRejectionMetadata(_Evidence):
    kind: Literal["definitivelyNotAdmitted"]
    submission_id: UUID = Field(alias="submissionId")
    owner_id: UUID = Field(alias="ownerId")
    attempted_body: dict[str, JsonValue] = Field(alias="attemptedBody")


class _CommandValidationRejection(_CommandValidationRejectionMetadata):
    command_version: Literal["1"] = Field(alias="commandVersion")


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


class _LedgerReceipt(_Receipt):
    operation: Literal["createFinanceLedger"]
    target_ledger_id: None = Field(alias="targetLedgerId")


class LedgerCreatedReceipt(_LedgerReceipt):
    outcome: LedgerCreatedOutcome


class LedgerRejectedReceipt(_LedgerReceipt):
    outcome: LedgerRejectedOutcome


class LedgerSubmissionReceipt(_LedgerReceipt):
    outcome: Annotated[LedgerCreatedOutcome | LedgerRejectedOutcome, Field(discriminator="kind")]


class LedgerSubmissionUnfinished(_Unfinished):
    operation: Literal["createFinanceLedger"]
    target_ledger_id: None = Field(alias="targetLedgerId")


class LedgerCommandValidationRejection(_CommandValidationRejection):
    operation: Literal["createFinanceLedger"]
    target_ledger_id: None = Field(alias="targetLedgerId")


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


class AccountCreatedResource(_Evidence):
    type: Literal["account"]
    id: UUID


class AccountCreatedOutcome(_Evidence):
    kind: Literal["created"]
    resource: AccountCreatedResource


class AccountRejectionProblem(_Evidence):
    type: str
    title: str
    status: Literal[422]
    code: Literal["validation_error"]
    detail: str


class AccountRejectedOutcome(_Evidence):
    kind: Literal["rejected"]
    problem: AccountRejectionProblem


class _AccountReceipt(_Receipt):
    operation: Literal["createFinanceAccount"]
    target_ledger_id: UUID = Field(alias="targetLedgerId")


class AccountCreatedReceipt(_AccountReceipt):
    outcome: AccountCreatedOutcome


class AccountRejectedReceipt(_AccountReceipt):
    outcome: AccountRejectedOutcome


class AccountSubmissionReceipt(_AccountReceipt):
    outcome: Annotated[AccountCreatedOutcome | AccountRejectedOutcome, Field(discriminator="kind")]


class AccountSubmissionUnfinished(_Unfinished):
    operation: Literal["createFinanceAccount"]
    target_ledger_id: UUID = Field(alias="targetLedgerId")


class AccountCommandValidationRejection(_CommandValidationRejection):
    operation: Literal["createFinanceAccount"]
    target_ledger_id: UUID = Field(alias="targetLedgerId")


class AccountTerminalProblem(ProblemDetails):
    model_config = ConfigDict(extra="forbid")
    status: Literal[422]
    code: Literal["validation_error"]
    submissionReceipt: AccountRejectedReceipt


class AccountValidationProblem(ProblemDetails):
    model_config = ConfigDict(extra="forbid")
    status: Literal[422]
    code: Literal["validation_error"]
    errors: list[dict[str, JsonValue]]
    commandValidationRejection: AccountCommandValidationRejection


class AccountValidationResponse(
    RootModel[SubmissionNonterminalProblem | AccountValidationProblem | AccountTerminalProblem]
):
    """Generic validation/version failure or correlated Q29 evidence."""


class CategoryCreatedResource(_Evidence):
    type: Literal["category"]
    id: UUID


class CategoryCreatedOutcome(_Evidence):
    kind: Literal["created"]
    resource: CategoryCreatedResource


class CategoryRejectionProblem(_Evidence):
    type: str
    title: str
    status: Literal[409]
    code: Literal["finance_category_name_conflict"]
    detail: str


class CategoryRejectedOutcome(_Evidence):
    kind: Literal["rejected"]
    problem: CategoryRejectionProblem


class _CategoryReceipt(_Receipt):
    operation: Literal["createFinanceCategory"]
    target_ledger_id: UUID = Field(alias="targetLedgerId")


class CategoryCreatedReceipt(_CategoryReceipt):
    outcome: CategoryCreatedOutcome


class CategoryRejectedReceipt(_CategoryReceipt):
    outcome: CategoryRejectedOutcome


class CategorySubmissionReceipt(_CategoryReceipt):
    outcome: Annotated[
        CategoryCreatedOutcome | CategoryRejectedOutcome, Field(discriminator="kind")
    ]


class CategorySubmissionUnfinished(_Unfinished):
    operation: Literal["createFinanceCategory"]
    target_ledger_id: UUID = Field(alias="targetLedgerId")


class CategoryCommandValidationRejection(_CommandValidationRejection):
    operation: Literal["createFinanceCategory"]
    target_ledger_id: UUID = Field(alias="targetLedgerId")


class CategoryTerminalProblem(ProblemDetails):
    model_config = ConfigDict(extra="forbid")
    status: Literal[409]
    code: Literal["finance_category_name_conflict"]
    submissionReceipt: CategoryRejectedReceipt


class CategoryValidationProblem(ProblemDetails):
    model_config = ConfigDict(extra="forbid")
    status: Literal[422]
    code: Literal["validation_error"]
    errors: list[dict[str, JsonValue]]
    commandValidationRejection: CategoryCommandValidationRejection


class CategoryConflictResponse(RootModel[SubmissionNonterminalProblem | CategoryTerminalProblem]):
    """Nonterminal protocol conflict or terminal business rejection."""


class CategoryValidationResponse(
    RootModel[SubmissionNonterminalProblem | CategoryValidationProblem]
):
    """Generic validation/version failure or correlated Q29 evidence."""


class TransactionCreatedResource(_Evidence):
    type: Literal["transaction"]
    id: UUID


class TransactionCreatedOutcome(_Evidence):
    kind: Literal["created"]
    resource: TransactionCreatedResource


class TransactionInvalidProblem(_Evidence):
    type: str
    title: str
    status: Literal[422]
    code: Literal["validation_error"]
    detail: str


class TransactionArchivedProblem(_Evidence):
    type: str
    title: str
    status: Literal[409]
    code: Literal[
        "finance_account_archived", "finance_category_archived", "finance_linked_record_protected"
    ]
    detail: str


class TransactionMissingProblem(_Evidence):
    type: str
    title: str
    status: Literal[404]
    code: Literal["finance_account_not_found", "finance_category_not_found"]
    detail: str


class TransactionRejectedOutcome(_Evidence):
    kind: Literal["rejected"]
    problem: Annotated[
        TransactionInvalidProblem | TransactionArchivedProblem | TransactionMissingProblem,
        Field(discriminator="code"),
    ]


class _TransactionReceipt(_Receipt):
    operation: Literal["createFinanceTransaction"]
    target_ledger_id: UUID = Field(alias="targetLedgerId")


class TransactionCreatedReceipt(_TransactionReceipt):
    outcome: TransactionCreatedOutcome


class TransactionRejectedReceipt(_TransactionReceipt):
    outcome: TransactionRejectedOutcome


class TransactionSubmissionReceipt(_TransactionReceipt):
    outcome: Annotated[
        TransactionCreatedOutcome | TransactionRejectedOutcome, Field(discriminator="kind")
    ]


class TransactionSubmissionUnfinished(_Unfinished):
    operation: Literal["createFinanceTransaction"]
    target_ledger_id: UUID = Field(alias="targetLedgerId")


class TransactionCommandValidationRejection(_CommandValidationRejection):
    operation: Literal["createFinanceTransaction"]
    target_ledger_id: UUID = Field(alias="targetLedgerId")


class TransactionInvalidOutcome(TransactionRejectedOutcome):
    problem: TransactionInvalidProblem


class TransactionArchivedOutcome(TransactionRejectedOutcome):
    problem: TransactionArchivedProblem


class TransactionMissingOutcome(TransactionRejectedOutcome):
    problem: TransactionMissingProblem


class TransactionInvalidReceipt(TransactionRejectedReceipt):
    outcome: TransactionInvalidOutcome


class TransactionArchivedReceipt(TransactionRejectedReceipt):
    outcome: TransactionArchivedOutcome


class TransactionMissingReceipt(TransactionRejectedReceipt):
    outcome: TransactionMissingOutcome


class TransactionInvalidTerminalProblem(ProblemDetails):
    model_config = ConfigDict(extra="forbid")
    status: Literal[422]
    code: Literal["validation_error"]
    submissionReceipt: TransactionInvalidReceipt


class TransactionArchivedTerminalProblem(ProblemDetails):
    model_config = ConfigDict(extra="forbid")
    status: Literal[409]
    code: Literal[
        "finance_account_archived", "finance_category_archived", "finance_linked_record_protected"
    ]
    submissionReceipt: TransactionArchivedReceipt


class TransactionMissingTerminalProblem(ProblemDetails):
    model_config = ConfigDict(extra="forbid")
    status: Literal[404]
    code: Literal["finance_account_not_found", "finance_category_not_found"]
    submissionReceipt: TransactionMissingReceipt


class TransactionValidationProblem(ProblemDetails):
    model_config = ConfigDict(extra="forbid")
    status: Literal[422]
    code: Literal["validation_error"]
    errors: list[dict[str, JsonValue]]
    commandValidationRejection: TransactionCommandValidationRejection


class TransactionConflictResponse(
    RootModel[SubmissionNonterminalProblem | TransactionArchivedTerminalProblem]
):
    """Unresolved failures or immutable Transaction business rejection."""


class TransactionNotFoundResponse(
    RootModel[SubmissionNonterminalProblem | TransactionMissingTerminalProblem]
):
    """Unavailable scope or immutable missing Account/Category rejection."""


class TransactionValidationResponse(
    RootModel[
        SubmissionNonterminalProblem
        | TransactionValidationProblem
        | TransactionInvalidTerminalProblem
    ]
):
    """Distinguish unresolved validation, Q29, and terminal business evidence."""


class AdjustmentCreatedResource(_Evidence):
    type: Literal["transaction"]
    id: UUID


class AdjustmentCreatedOutcome(_Evidence):
    kind: Literal["created"]
    resource: AdjustmentCreatedResource


class AdjustmentInvalidProblem(_Evidence):
    type: str
    title: str
    status: Literal[422]
    code: Literal["validation_error"]
    detail: str


class AdjustmentConflictProblem(_Evidence):
    type: str
    title: str
    status: Literal[409]
    code: Literal[
        "finance_account_archived",
        "account_balance_changed",
        "finance_account_semantics_changed",
        "finance_linked_record_protected",
    ]
    detail: str


class AdjustmentMissingProblem(_Evidence):
    type: str
    title: str
    status: Literal[404]
    code: Literal["finance_account_not_found"]
    detail: str


class AdjustmentRejectedOutcome(_Evidence):
    kind: Literal["rejected"]
    problem: Annotated[
        AdjustmentInvalidProblem | AdjustmentConflictProblem | AdjustmentMissingProblem,
        Field(discriminator="code"),
    ]


class _AdjustmentReceipt(_Receipt):
    operation: Literal["createBalanceAdjustment"]
    target_ledger_id: UUID = Field(alias="targetLedgerId")


class AdjustmentNoChangeOutcome(_Evidence):
    kind: Literal["noChange"]


class AdjustmentSuccessReceipt(_AdjustmentReceipt):
    outcome: Annotated[
        AdjustmentCreatedOutcome | AdjustmentNoChangeOutcome, Field(discriminator="kind")
    ]


class AdjustmentRejectedReceipt(_AdjustmentReceipt):
    outcome: AdjustmentRejectedOutcome


class AdjustmentSubmissionReceipt(_AdjustmentReceipt):
    outcome: Annotated[
        AdjustmentCreatedOutcome | AdjustmentNoChangeOutcome | AdjustmentRejectedOutcome,
        Field(discriminator="kind"),
    ]


class AdjustmentSubmissionUnfinished(_Unfinished):
    operation: Literal["createBalanceAdjustment"]
    target_ledger_id: UUID = Field(alias="targetLedgerId")


class AdjustmentCommandValidationRejection(_CommandValidationRejection):
    operation: Literal["createBalanceAdjustment"]
    target_ledger_id: UUID = Field(alias="targetLedgerId")


class AdjustmentInvalidOutcome(AdjustmentRejectedOutcome):
    problem: AdjustmentInvalidProblem


class AdjustmentConflictOutcome(AdjustmentRejectedOutcome):
    problem: AdjustmentConflictProblem


class AdjustmentMissingOutcome(AdjustmentRejectedOutcome):
    problem: AdjustmentMissingProblem


class AdjustmentInvalidReceipt(AdjustmentRejectedReceipt):
    outcome: AdjustmentInvalidOutcome


class AdjustmentConflictReceipt(AdjustmentRejectedReceipt):
    outcome: AdjustmentConflictOutcome


class AdjustmentMissingReceipt(AdjustmentRejectedReceipt):
    outcome: AdjustmentMissingOutcome


class AdjustmentInvalidTerminalProblem(ProblemDetails):
    model_config = ConfigDict(extra="forbid")
    status: Literal[422]
    code: Literal["validation_error"]
    submissionReceipt: AdjustmentInvalidReceipt


class AdjustmentConflictTerminalProblem(ProblemDetails):
    model_config = ConfigDict(extra="forbid")
    status: Literal[409]
    code: Literal[
        "finance_account_archived",
        "account_balance_changed",
        "finance_account_semantics_changed",
        "finance_linked_record_protected",
    ]
    submissionReceipt: AdjustmentConflictReceipt


class AdjustmentMissingTerminalProblem(ProblemDetails):
    model_config = ConfigDict(extra="forbid")
    status: Literal[404]
    code: Literal["finance_account_not_found"]
    submissionReceipt: AdjustmentMissingReceipt


class AdjustmentValidationProblem(ProblemDetails):
    model_config = ConfigDict(extra="forbid")
    status: Literal[422]
    code: Literal["validation_error"]
    errors: list[dict[str, JsonValue]]
    commandValidationRejection: AdjustmentCommandValidationRejection


class AdjustmentConflictResponse(
    RootModel[SubmissionNonterminalProblem | AdjustmentConflictTerminalProblem]
):
    """Unresolved failures or immutable Adjustment business rejection."""


class AdjustmentNotFoundResponse(
    RootModel[SubmissionNonterminalProblem | AdjustmentMissingTerminalProblem]
):
    """Unavailable scope or immutable missing Account rejection."""


class AdjustmentValidationResponse(
    RootModel[
        SubmissionNonterminalProblem
        | AdjustmentValidationProblem
        | AdjustmentInvalidTerminalProblem
    ]
):
    """Distinguish unresolved validation, Q29, and terminal business evidence."""


class AdvanceCreatedResource(_Evidence):
    type: Literal["advance"]
    id: UUID


class AdvanceCreatedOutcome(_Evidence):
    kind: Literal["created"]
    resource: AdvanceCreatedResource


class AdvanceInvalidProblem(TransactionInvalidProblem):
    pass


class AdvanceConflictProblem(_Evidence):
    type: str
    title: str
    status: Literal[409]
    code: Literal[
        "finance_account_archived",
        "finance_participant_archived",
        "finance_advance_preview_stale",
        "finance_linked_record_protected",
    ]
    detail: str


class AdvanceMissingProblem(_Evidence):
    type: str
    title: str
    status: Literal[404]
    code: Literal["finance_account_not_found", "finance_participant_not_found"]
    detail: str


class AdvanceRejectedOutcome(_Evidence):
    kind: Literal["rejected"]
    problem: Annotated[
        AdvanceInvalidProblem | AdvanceConflictProblem | AdvanceMissingProblem,
        Field(discriminator="code"),
    ]


class _AdvanceReceipt(_ReceiptMetadata):
    command_version: Literal["1", "2"] = Field(alias="commandVersion")
    operation: Literal["createFinanceAdvance"]
    target_ledger_id: UUID = Field(alias="targetLedgerId")


class AdvanceCreatedReceipt(_AdvanceReceipt):
    outcome: AdvanceCreatedOutcome


class AdvanceRejectedReceipt(_AdvanceReceipt):
    outcome: AdvanceRejectedOutcome


class AdvanceSubmissionReceipt(_AdvanceReceipt):
    outcome: Annotated[AdvanceCreatedOutcome | AdvanceRejectedOutcome, Field(discriminator="kind")]


class AdvanceSubmissionUnfinished(_UnfinishedMetadata):
    command_version: Literal["1", "2"] = Field(alias="commandVersion")
    operation: Literal["createFinanceAdvance"]
    target_ledger_id: UUID = Field(alias="targetLedgerId")


class AdvanceCommandValidationRejection(_CommandValidationRejectionMetadata):
    command_version: Literal["1", "2"] = Field(alias="commandVersion")
    operation: Literal["createFinanceAdvance"]
    target_ledger_id: UUID = Field(alias="targetLedgerId")


class AdvanceValidationProblem(ProblemDetails):
    model_config = ConfigDict(extra="forbid")
    status: Literal[422]
    code: Literal["validation_error"]
    errors: list[dict[str, JsonValue]]
    commandValidationRejection: AdvanceCommandValidationRejection


class AdvanceTerminalProblem(ProblemDetails):
    model_config = ConfigDict(extra="forbid")
    status: Literal[404, 409, 422]
    code: Literal[
        "validation_error",
        "finance_account_archived",
        "finance_participant_archived",
        "finance_advance_preview_stale",
        "finance_linked_record_protected",
        "finance_account_not_found",
        "finance_participant_not_found",
    ]
    submissionReceipt: AdvanceRejectedReceipt


class AdvanceFailureResponse(
    RootModel[SubmissionNonterminalProblem | AdvanceValidationProblem | AdvanceTerminalProblem]
):
    """Unresolved failure, guarded pre-admission proof, or terminal rejection."""


class SettlementCreatedResource(_Evidence):
    type: Literal["settlement"]
    id: UUID


class SettlementCreatedOutcome(_Evidence):
    kind: Literal["created"]
    resource: SettlementCreatedResource


class SettlementInvalidProblem(TransactionInvalidProblem):
    pass


class SettlementConflictProblem(_Evidence):
    type: str
    title: str
    status: Literal[409]
    code: Literal[
        "finance_account_archived",
        "finance_settlement_preview_stale",
        "finance_linked_record_protected",
    ]
    detail: str


class SettlementMissingProblem(_Evidence):
    type: str
    title: str
    status: Literal[404]
    code: Literal["finance_account_not_found", "finance_participant_not_found"]
    detail: str


class SettlementRejectedOutcome(_Evidence):
    kind: Literal["rejected"]
    problem: Annotated[
        SettlementInvalidProblem | SettlementConflictProblem | SettlementMissingProblem,
        Field(discriminator="code"),
    ]


class _SettlementReceipt(_ReceiptMetadata):
    command_version: Literal["1", "2"] = Field(alias="commandVersion")
    operation: Literal["createFinanceSettlement"]
    target_ledger_id: UUID = Field(alias="targetLedgerId")


class SettlementCreatedReceipt(_SettlementReceipt):
    outcome: SettlementCreatedOutcome


class SettlementRejectedReceipt(_SettlementReceipt):
    outcome: SettlementRejectedOutcome


class SettlementSubmissionReceipt(_SettlementReceipt):
    outcome: Annotated[
        SettlementCreatedOutcome | SettlementRejectedOutcome, Field(discriminator="kind")
    ]


class SettlementSubmissionUnfinished(_UnfinishedMetadata):
    command_version: Literal["1", "2"] = Field(alias="commandVersion")
    operation: Literal["createFinanceSettlement"]
    target_ledger_id: UUID = Field(alias="targetLedgerId")


class SettlementCommandValidationRejection(_CommandValidationRejectionMetadata):
    command_version: Literal["1", "2"] = Field(alias="commandVersion")
    operation: Literal["createFinanceSettlement"]
    target_ledger_id: UUID = Field(alias="targetLedgerId")


class SettlementValidationProblem(ProblemDetails):
    model_config = ConfigDict(extra="forbid")
    status: Literal[422]
    code: Literal["validation_error"]
    errors: list[dict[str, JsonValue]]
    commandValidationRejection: SettlementCommandValidationRejection


class SettlementTerminalProblem(ProblemDetails):
    model_config = ConfigDict(extra="forbid")
    status: Literal[404, 409, 422]
    code: Literal[
        "validation_error",
        "finance_account_archived",
        "finance_settlement_preview_stale",
        "finance_linked_record_protected",
        "finance_account_not_found",
        "finance_participant_not_found",
    ]
    submissionReceipt: SettlementRejectedReceipt


class SettlementFailureResponse(
    RootModel[
        SubmissionNonterminalProblem | SettlementValidationProblem | SettlementTerminalProblem
    ]
):
    """Unresolved failure, guarded pre-admission proof, or terminal rejection."""


type SubmissionReceipt = Annotated[
    LedgerSubmissionReceipt
    | AccountSubmissionReceipt
    | CategorySubmissionReceipt
    | TransactionSubmissionReceipt
    | AdjustmentSubmissionReceipt
    | AdvanceSubmissionReceipt
    | SettlementSubmissionReceipt,
    Field(discriminator="operation"),
]
type SubmissionValidationProblem = (
    LedgerValidationProblem
    | AccountValidationProblem
    | CategoryValidationProblem
    | TransactionValidationProblem
    | AdjustmentValidationProblem
    | AdvanceValidationProblem
    | SettlementValidationProblem
)

type SubmissionUnfinished = Annotated[
    LedgerSubmissionUnfinished
    | AccountSubmissionUnfinished
    | CategorySubmissionUnfinished
    | TransactionSubmissionUnfinished
    | AdjustmentSubmissionUnfinished
    | AdvanceSubmissionUnfinished
    | SettlementSubmissionUnfinished,
    Field(discriminator="operation"),
]


class FinanceSubmissionTerminal(_Evidence):
    state: Literal["terminal"]
    receipt: SubmissionReceipt


class FinanceSubmissionResponse(
    RootModel[
        Annotated[SubmissionUnfinished | FinanceSubmissionTerminal, Field(discriminator="state")]
    ]
):
    """Known-ID lookup, deliberately excluding canonical content."""
