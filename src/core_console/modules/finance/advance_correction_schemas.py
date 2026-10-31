"""Closed original-fact correction intent, complete plans and retained history."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from core_console.modules.finance.advance_schemas import (
    AdvanceDraftV2,
    AdvanceResponse,
    ParticipantShareV2,
    SharedAdvanceResponse,
    _Projection,
)
from core_console.modules.finance.schemas import MoneyResponse


class AdvanceCorrectionDraftV1(AdvanceDraftV2):
    participant_shares: list[ParticipantShareV2] = Field(alias="participantShares")
    advance_id: UUID = Field(alias="advanceId")
    reason: str = Field(strict=True, json_schema_extra={"maxLength": 500})

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value: str) -> str:
        value = value.strip()
        if not 1 <= len(value) <= 500:
            raise ValueError("Correction reason must contain 1 to 500 characters.")
        return value

    def canonical(self) -> dict[str, object]:
        return {**super().canonical(), "advanceId": str(self.advance_id), "reason": self.reason}


class AdvanceCorrectionCommandV1(AdvanceCorrectionDraftV1):
    preview_token: str = Field(alias="previewToken", strict=True, pattern=r"^[0-9a-f]{64}$")

    def canonical(self) -> dict[str, object]:
        return {**super().canonical(), "previewToken": self.preview_token}


class CorrectionMovement(_Projection):
    account_id: UUID = Field(alias="accountId")
    amount: MoneyResponse
    role: Literal["source", "destination", "primary"]


class CorrectionFinancialEffect(_Projection):
    transaction_id: UUID = Field(alias="transactionId")
    kind: Literal["expense", "internalTransfer"]
    transaction_date: date = Field(alias="transactionDate")
    note: str | None
    movements: list[CorrectionMovement]
    owner_expense: MoneyResponse = Field(alias="ownerExpense")


class CorrectionAccountEffect(_Projection):
    account_id: UUID = Field(alias="accountId")
    nature: Literal["asset", "liability"]
    balance_before: MoneyResponse = Field(alias="balanceBefore")
    balance_after: MoneyResponse = Field(alias="balanceAfter")


class AdvanceCorrectionPreview(_Projection):
    preview_token: str = Field(alias="previewToken")
    advance_id: UUID = Field(alias="advanceId")
    reason: str
    funding_changed: bool = Field(alias="fundingChanged")
    before: AdvanceResponse | SharedAdvanceResponse
    after: SharedAdvanceResponse
    financial_effects_before: list[CorrectionFinancialEffect] = Field(
        alias="financialEffectsBefore"
    )
    financial_effects_after: list[CorrectionFinancialEffect] = Field(alias="financialEffectsAfter")
    account_effects: list[CorrectionAccountEffect] = Field(alias="accountEffects")


class AdvanceCorrectionResponse(AdvanceCorrectionPreview):
    id: UUID
    ledger_id: UUID = Field(alias="ledgerId")
    actor_id: UUID = Field(alias="actorId")
    confirmed_at: datetime = Field(alias="confirmedAt")
    revision: int
