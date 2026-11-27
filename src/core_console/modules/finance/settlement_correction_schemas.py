"""Closed Settlement Correction intent and complete retained consequences."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core_console.modules.finance.advance_correction_schemas import (
    CorrectionAccountEffect,
    CorrectionCreditReturn,
    CorrectionMovement,
    CorrectionParticipantPosition,
    ReceiptCreditApplication,
)
from core_console.modules.finance.advance_schemas import ObligationResponse, _Projection
from core_console.modules.finance.schemas import MoneyResponse
from core_console.modules.finance.settlement_schemas import CreditOriginResponse
from core_console.modules.finance.submission_commands import OpeningBalanceV1


class SettlementCorrectionDraftV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    settlement_id: UUID = Field(alias="settlementId")
    amount: OpeningBalanceV1
    reason: str = Field(strict=True, json_schema_extra={"maxLength": 500})
    excess_disposition: Literal["none", "retainCredit", "acceptIncome"] = Field(
        alias="excessDisposition"
    )
    category_id: UUID | None = Field(alias="categoryId")

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value: str) -> str:
        value = value.strip()
        if not 1 <= len(value) <= 500:
            raise ValueError("Correction reason must contain 1 to 500 characters.")
        return value

    @model_validator(mode="after")
    def valid_receipt(self) -> Self:
        if Decimal(self.amount.amount) <= 0:
            raise ValueError("Corrected cash receipt must be positive.")
        if self.excess_disposition != "acceptIncome" and self.category_id is not None:
            raise ValueError("Only accepted surplus Income has a Category.")
        return self

    def canonical(self) -> dict[str, object]:
        return {
            "settlementId": str(self.settlement_id),
            "amount": self.amount.canonical(),
            "reason": self.reason,
            "excessDisposition": self.excess_disposition,
            "categoryId": str(self.category_id) if self.category_id else None,
        }


class SettlementCorrectionCommandV1(SettlementCorrectionDraftV1):
    preview_token: str = Field(alias="previewToken", strict=True, pattern=r"^[0-9a-f]{64}$")

    def canonical(self) -> dict[str, object]:
        return {**super().canonical(), "previewToken": self.preview_token}


class SettlementCorrectionDraftV2(SettlementCorrectionDraftV1):
    forgiveness_amount: OpeningBalanceV1 = Field(alias="forgivenessAmount")

    @model_validator(mode="after")
    def valid_receipt(self) -> Self:
        cash = Decimal(self.amount.amount)
        forgiven = Decimal(self.forgiveness_amount.amount)
        if min(cash, forgiven) < 0 or max(cash, forgiven) == 0:
            raise ValueError("Enter non-negative amounts with at least one positive amount.")
        if self.amount.currency != self.forgiveness_amount.currency:
            raise ValueError("Cash and forgiveness must use the same currency.")
        if self.excess_disposition != "acceptIncome" and self.category_id is not None:
            raise ValueError("Only accepted surplus Income has a Category.")
        return self

    def canonical(self) -> dict[str, object]:
        return {**super().canonical(), "forgivenessAmount": self.forgiveness_amount.canonical()}


class SettlementCorrectionCommandV2(SettlementCorrectionCommandV1, SettlementCorrectionDraftV2):
    pass


class CorrectionCashAllocation(_Projection):
    position: int
    obligation_id: UUID = Field(alias="obligationId")
    original_cash_paid: MoneyResponse = Field(alias="originalCashPaid")
    cash_paid: MoneyResponse = Field(alias="cashPaid")
    original_forgiven: MoneyResponse | None = Field(
        default=None, alias="originalForgiven", exclude_if=lambda value: value is None
    )
    forgiven: MoneyResponse | None = Field(default=None, exclude_if=lambda value: value is None)
    expense_transaction_id: UUID | None = Field(
        default=None, alias="expenseTransactionId", exclude_if=lambda value: value is None
    )


class ReceiptCorrectionFacts(_Projection):
    amount: MoneyResponse
    forgiveness_amount: MoneyResponse | None = Field(
        default=None, alias="forgivenessAmount", exclude_if=lambda value: value is None
    )
    allocations: list[CorrectionCashAllocation]
    retained_credit: MoneyResponse = Field(alias="retainedCredit")
    accepted_surplus: MoneyResponse = Field(alias="acceptedSurplus")
    category_id: UUID | None = Field(alias="categoryId")


class ReceiptFinancialEffect(_Projection):
    transaction_id: UUID = Field(alias="transactionId")
    kind: Literal["internalTransfer", "income", "expense"]
    transaction_date: date = Field(alias="transactionDate")
    note: str | None
    movements: list[CorrectionMovement]
    income: MoneyResponse
    expense: MoneyResponse | None = Field(default=None, exclude_if=lambda value: value is None)
    category_id: UUID | None = Field(alias="categoryId")


class ReceiptCreditCorrection(_Projection):
    origin: CreditOriginResponse
    available_after: MoneyResponse = Field(alias="availableAfter")
    applications: list[ReceiptCreditApplication]
    participant_position: CorrectionParticipantPosition = Field(alias="participantPosition")
    returns: list[CorrectionCreditReturn] = Field(
        default_factory=list, exclude_if=lambda value: not value
    )
    direct_cash_released_for_returns: MoneyResponse | None = Field(
        default=None, alias="directCashReleasedForReturns", exclude_if=lambda value: value is None
    )


class SettlementCorrectionPreview(_Projection):
    preview_token: str = Field(alias="previewToken")
    settlement_id: UUID = Field(alias="settlementId")
    participant_id: UUID = Field(alias="participantId")
    receiving_account_id: UUID | None = Field(alias="receivingAccountId")
    transaction_date: date = Field(alias="transactionDate")
    original_confirmed_at: datetime = Field(alias="originalConfirmedAt")
    confirmation_order: int = Field(alias="confirmationOrder")
    selected_obligation_ids: list[UUID] | None = Field(alias="selectedObligationIds")
    reason: str
    excess_disposition: Literal["none", "retainCredit", "acceptIncome"] = Field(
        alias="excessDisposition"
    )
    before: ReceiptCorrectionFacts
    after: ReceiptCorrectionFacts
    obligations_before: list[ObligationResponse] = Field(alias="obligationsBefore")
    obligations_after: list[ObligationResponse] = Field(alias="obligationsAfter")
    financial_effects_before: list[ReceiptFinancialEffect] = Field(alias="financialEffectsBefore")
    financial_effects_after: list[ReceiptFinancialEffect] = Field(alias="financialEffectsAfter")
    account_effects: list[CorrectionAccountEffect] = Field(alias="accountEffects")
    credit_correction: ReceiptCreditCorrection | None = Field(
        default=None, alias="creditCorrection", exclude_if=lambda value: value is None
    )


class SettlementCorrectionResponse(SettlementCorrectionPreview):
    id: UUID
    ledger_id: UUID = Field(alias="ledgerId")
    actor_id: UUID = Field(alias="actorId")
    confirmed_at: datetime = Field(alias="confirmedAt")
    revision: int
