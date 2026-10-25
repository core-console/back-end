"""Frozen cash repayment intent and retained Allocation projections."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from core_console.modules.finance.advance_schemas import _Projection
from core_console.modules.finance.schemas import MoneyResponse
from core_console.modules.finance.submission_commands import (
    AllocationV1,
    OpeningBalanceV1,
    _TransactionCommandV1,
)


class SettlementDraftV1(_TransactionCommandV1):
    participant_id: UUID = Field(alias="participantId")
    receiving_account_id: UUID = Field(alias="receivingAccountId")
    amount: OpeningBalanceV1

    @model_validator(mode="after")
    def positive_receipt(self) -> Self:
        if Decimal(self.amount.amount) <= 0:
            raise ValueError("Cash repayment must be positive.")
        return self

    def canonical(self) -> dict[str, object]:
        return {
            "participantId": str(self.participant_id),
            "receivingAccountId": str(self.receiving_account_id),
            "transactionDate": self.transaction_date.isoformat(),
            "amount": self.amount.canonical(),
            "note": self.note,
        }


class SettlementCommandV1(SettlementDraftV1):
    preview_token: str = Field(alias="previewToken", strict=True, pattern=r"^[0-9a-f]{64}$")

    def canonical(self) -> dict[str, object]:
        return {**super().canonical(), "previewToken": self.preview_token}


class SettlementDraftV2(SettlementDraftV1):
    excess_disposition: Literal["retainCredit"] = Field(alias="excessDisposition")

    def canonical(self) -> dict[str, object]:
        return {**super().canonical(), "excessDisposition": self.excess_disposition}


class SettlementCommandV2(SettlementDraftV2):
    preview_token: str = Field(alias="previewToken", strict=True, pattern=r"^[0-9a-f]{64}$")

    def canonical(self) -> dict[str, object]:
        return {**super().canonical(), "previewToken": self.preview_token}


class SettlementDraftV3(SettlementDraftV1):
    excess_disposition: Literal["acceptIncome"] = Field(alias="excessDisposition")
    category_allocations: list[AllocationV1] = Field(
        alias="categoryAllocations", min_length=1, max_length=1
    )

    @model_validator(mode="after")
    def positive_income_classification(self) -> Self:
        amount = self.category_allocations[0].amount
        if amount.currency != self.amount.currency or not (
            0 < Decimal(amount.amount) <= Decimal(self.amount.amount)
        ):
            raise ValueError("Income classification must be positive and use the receipt currency.")
        return self

    def canonical(self) -> dict[str, object]:
        allocation = self.category_allocations[0]
        return {
            **super().canonical(),
            "excessDisposition": self.excess_disposition,
            "categoryAllocations": [
                {
                    "amount": allocation.amount.canonical(),
                    "categoryId": str(allocation.category_id) if allocation.category_id else None,
                }
            ],
        }


class SettlementCommandV3(SettlementDraftV3):
    preview_token: str = Field(alias="previewToken", strict=True, pattern=r"^[0-9a-f]{64}$")

    def canonical(self) -> dict[str, object]:
        return {**super().canonical(), "previewToken": self.preview_token}


class SelectedScope(BaseModel):
    selected_obligation_ids: list[UUID] = Field(alias="selectedObligationIds", min_length=1)

    @model_validator(mode="after")
    def unique_selection(self) -> Self:
        if len(set(self.selected_obligation_ids)) != len(self.selected_obligation_ids):
            raise ValueError("Select each Obligation only once.")
        return self


class ForgivenessDraftV5(_TransactionCommandV1, SelectedScope):
    kind: Literal["forgiveness"]
    participant_id: UUID = Field(alias="participantId")
    amount: OpeningBalanceV1
    reason: str = Field(strict=True, min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def explicit_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("A forgiveness reason is required.")
        return value

    @model_validator(mode="after")
    def positive_forgiveness(self) -> Self:
        if Decimal(self.amount.amount) <= 0:
            raise ValueError("Forgiveness must be positive.")
        return self

    def canonical(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "participantId": str(self.participant_id),
            "selectedObligationIds": sorted(str(id) for id in self.selected_obligation_ids),
            "transactionDate": self.transaction_date.isoformat(),
            "amount": self.amount.canonical(),
            "reason": self.reason,
            "note": self.note,
        }


class ForgivenessCommandV5(ForgivenessDraftV5):
    preview_token: str = Field(alias="previewToken", strict=True, pattern=r"^[0-9a-f]{64}$")

    def canonical(self) -> dict[str, object]:
        return {**super().canonical(), "previewToken": self.preview_token}


class ForgivenessAllocationResponse(_Projection):
    kind: Literal["forgiveness"] = "forgiveness"
    position: int
    obligation_id: UUID = Field(alias="obligationId")
    advance_id: UUID = Field(alias="advanceId")
    source_date: date = Field(alias="sourceDate")
    forgiven: MoneyResponse
    remaining_after: MoneyResponse = Field(alias="remainingAfter")


class ForgivenessPreview(_Projection):
    preview_token: str = Field(alias="previewToken")
    amount: MoneyResponse
    expense: MoneyResponse
    outstanding_before: MoneyResponse = Field(alias="outstandingBefore")
    outstanding_after: MoneyResponse = Field(alias="outstandingAfter")
    allocations: list[ForgivenessAllocationResponse]


class ForgivenessResponse(_Projection, SelectedScope):
    kind: Literal["forgiveness"] = "forgiveness"
    id: UUID
    ledger_id: UUID = Field(alias="ledgerId")
    participant_id: UUID = Field(alias="participantId")
    transaction_date: date = Field(alias="transactionDate")
    amount: MoneyResponse
    reason: str
    note: str | None
    confirmed_at: datetime = Field(alias="confirmedAt")
    confirmation_order: int = Field(alias="confirmationOrder")
    transaction_ids: list[UUID] = Field(alias="transactionIds")
    allocations: list[ForgivenessAllocationResponse]


class SelectedSettlementDraftV4(SettlementDraftV1, SelectedScope):
    def canonical(self) -> dict[str, object]:
        return {
            **super().canonical(),
            "selectedObligationIds": sorted(str(id) for id in self.selected_obligation_ids),
        }


class SelectedCreditSettlementDraftV4(SettlementDraftV2, SelectedScope):
    def canonical(self) -> dict[str, object]:
        return {
            **super().canonical(),
            "selectedObligationIds": sorted(str(id) for id in self.selected_obligation_ids),
        }


class SelectedIncomeSettlementDraftV4(SettlementDraftV3, SelectedScope):
    def canonical(self) -> dict[str, object]:
        return {
            **super().canonical(),
            "selectedObligationIds": sorted(str(id) for id in self.selected_obligation_ids),
        }


class SelectedSettlementCommandV4(SelectedSettlementDraftV4):
    preview_token: str = Field(alias="previewToken", strict=True, pattern=r"^[0-9a-f]{64}$")

    def canonical(self) -> dict[str, object]:
        return {**super().canonical(), "previewToken": self.preview_token}


class SelectedCreditSettlementCommandV4(SelectedCreditSettlementDraftV4):
    preview_token: str = Field(alias="previewToken", strict=True, pattern=r"^[0-9a-f]{64}$")

    def canonical(self) -> dict[str, object]:
        return {**super().canonical(), "previewToken": self.preview_token}


class SelectedIncomeSettlementCommandV4(SelectedIncomeSettlementDraftV4):
    preview_token: str = Field(alias="previewToken", strict=True, pattern=r"^[0-9a-f]{64}$")

    def canonical(self) -> dict[str, object]:
        return {**super().canonical(), "previewToken": self.preview_token}


type SettlementDraftV4 = (
    SelectedSettlementDraftV4 | SelectedCreditSettlementDraftV4 | SelectedIncomeSettlementDraftV4
)
type SettlementCommandV4 = (
    SelectedSettlementCommandV4
    | SelectedCreditSettlementCommandV4
    | SelectedIncomeSettlementCommandV4
)


class CashAllocationResponse(_Projection):
    position: int
    obligation_id: UUID = Field(alias="obligationId")
    advance_id: UUID = Field(alias="advanceId")
    source_date: date = Field(alias="sourceDate")
    cash_paid: MoneyResponse = Field(alias="cashPaid")
    remaining_after: MoneyResponse = Field(alias="remainingAfter")


class SettlementPreview(_Projection):
    preview_token: str = Field(alias="previewToken")
    receiving_balance: MoneyResponse = Field(alias="receivingBalance")
    receiving_balance_after: MoneyResponse = Field(alias="receivingBalanceAfter")
    amount: MoneyResponse
    outstanding_before: MoneyResponse = Field(alias="outstandingBefore")
    outstanding_after: MoneyResponse = Field(alias="outstandingAfter")
    allocations: list[CashAllocationResponse]


class CreditSettlementPreview(SettlementPreview):
    retained_credit: MoneyResponse = Field(alias="retainedCredit")
    available_credit_before: MoneyResponse = Field(alias="availableCreditBefore")
    available_credit_after: MoneyResponse = Field(alias="availableCreditAfter")


class IncomeSettlementPreview(SettlementPreview):
    accepted_surplus: MoneyResponse = Field(alias="acceptedSurplus")
    category_allocations: list[AllocationV1] = Field(alias="categoryAllocations")
    available_credit_before: MoneyResponse = Field(alias="availableCreditBefore")
    available_credit_after: MoneyResponse = Field(alias="availableCreditAfter")


class CreditOriginResponse(_Projection):
    settlement_id: UUID = Field(alias="settlementId")
    ledger_id: UUID = Field(alias="ledgerId")
    participant_id: UUID = Field(alias="participantId")
    receiving_account_id: UUID = Field(alias="receivingAccountId")
    liability_account_id: UUID = Field(alias="liabilityAccountId")
    transaction_id: UUID = Field(alias="transactionId")
    transaction_date: date = Field(alias="transactionDate")
    confirmed_at: datetime = Field(alias="confirmedAt")
    confirmation_order: int = Field(alias="confirmationOrder")
    amount: MoneyResponse
    available: MoneyResponse


class SettlementResponse(_Projection):
    id: UUID
    ledger_id: UUID = Field(alias="ledgerId")
    participant_id: UUID = Field(alias="participantId")
    receiving_account_id: UUID = Field(alias="receivingAccountId")
    transaction_date: date = Field(alias="transactionDate")
    amount: MoneyResponse
    note: str | None
    confirmed_at: datetime = Field(alias="confirmedAt")
    confirmation_order: int = Field(alias="confirmationOrder")
    transaction_ids: list[UUID] = Field(alias="transactionIds")
    allocations: list[CashAllocationResponse]


class CreditSettlementResponse(SettlementResponse):
    credit_origin: CreditOriginResponse = Field(alias="creditOrigin")


class IncomeSettlementResponse(SettlementResponse):
    accepted_surplus: MoneyResponse = Field(alias="acceptedSurplus")
    income_transaction_id: UUID = Field(alias="incomeTransactionId")


class ParticipantPositionResponse(_Projection):
    currency: str
    outstanding_receivables: MoneyResponse = Field(alias="outstandingReceivables")
    available_credits: MoneyResponse = Field(alias="availableCredits")
    net_exposure: MoneyResponse = Field(alias="netExposure")


class SelectedSettlementResponse(SettlementResponse, SelectedScope):
    pass


class SelectedCreditSettlementResponse(CreditSettlementResponse, SelectedScope):
    pass


class SelectedIncomeSettlementResponse(IncomeSettlementResponse, SelectedScope):
    pass


type SettlementSource = (
    ForgivenessResponse
    | SelectedIncomeSettlementResponse
    | SelectedCreditSettlementResponse
    | SelectedSettlementResponse
    | IncomeSettlementResponse
    | CreditSettlementResponse
    | SettlementResponse
)
