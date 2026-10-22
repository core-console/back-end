"""Frozen cash repayment intent and retained Allocation projections."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from core_console.modules.finance.advance_schemas import _Projection
from core_console.modules.finance.schemas import MoneyResponse
from core_console.modules.finance.submission_commands import OpeningBalanceV1, _TransactionCommandV1


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


class ParticipantPositionResponse(_Projection):
    currency: str
    outstanding_receivables: MoneyResponse = Field(alias="outstandingReceivables")
    available_credits: MoneyResponse = Field(alias="availableCredits")
    net_exposure: MoneyResponse = Field(alias="netExposure")
