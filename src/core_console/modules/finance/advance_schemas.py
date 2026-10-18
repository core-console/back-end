"""T02's bounded public draft, confirmation command and read projections."""

from datetime import date, datetime
from decimal import Decimal
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core_console.modules.finance.schemas import MoneyResponse
from core_console.modules.finance.submission_commands import OpeningBalanceV1, _TransactionCommandV1


class AdvanceDraftV1(_TransactionCommandV1):
    funding_account_id: UUID = Field(alias="fundingAccountId")
    participant_id: UUID = Field(alias="participantId")
    amount: OpeningBalanceV1
    owner_share: OpeningBalanceV1 = Field(alias="ownerShare")
    participant_share: OpeningBalanceV1 = Field(alias="participantShare")

    @model_validator(mode="after")
    def pure_advance(self) -> Self:
        if Decimal(self.amount.amount) <= 0:
            raise ValueError("Advance funding must be positive.")
        if Decimal(self.owner_share.amount) != 0:
            raise ValueError("T02 supports only a zero owner share.")
        if (
            self.owner_share.currency != self.amount.currency
            or self.participant_share.currency != self.amount.currency
        ):
            raise ValueError("Funding and shares must use one currency.")
        if Decimal(self.participant_share.amount) != Decimal(self.amount.amount):
            raise ValueError("The participant share must equal the complete funded amount.")
        return self

    def canonical(self) -> dict[str, object]:
        return {
            "fundingAccountId": str(self.funding_account_id),
            "participantId": str(self.participant_id),
            "transactionDate": self.transaction_date.isoformat(),
            "amount": self.amount.canonical(),
            "ownerShare": self.owner_share.canonical(),
            "participantShare": self.participant_share.canonical(),
            "note": self.note,
        }


class AdvanceCommandV1(AdvanceDraftV1):
    preview_token: str = Field(alias="previewToken", strict=True, pattern=r"^[0-9a-f]{64}$")

    def canonical(self) -> dict[str, object]:
        return {**super().canonical(), "previewToken": self.preview_token}


class _Projection(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class AdvancePreview(_Projection):
    preview_token: str = Field(alias="previewToken")
    funding_balance: MoneyResponse = Field(alias="fundingBalance")
    funding_balance_after: MoneyResponse = Field(alias="fundingBalanceAfter")
    owner_expense: MoneyResponse = Field(alias="ownerExpense")
    receivable_increase: MoneyResponse = Field(alias="receivableIncrease")


class ObligationResponse(_Projection):
    id: UUID
    ledger_id: UUID = Field(alias="ledgerId")
    advance_id: UUID = Field(alias="advanceId")
    participant_id: UUID = Field(alias="participantId")
    source_date: date = Field(alias="sourceDate")
    receivable_account_id: UUID = Field(alias="receivableAccountId")
    principal: MoneyResponse
    cash_paid: MoneyResponse = Field(alias="cashPaid")
    forgiven: MoneyResponse
    credit_applied: MoneyResponse = Field(alias="creditApplied")
    remaining: MoneyResponse


class AdvanceResponse(_Projection):
    id: UUID
    ledger_id: UUID = Field(alias="ledgerId")
    funding_account_id: UUID = Field(alias="fundingAccountId")
    transaction_id: UUID = Field(alias="transactionId")
    transaction_date: date = Field(alias="transactionDate")
    note: str | None
    confirmed_at: datetime = Field(alias="confirmedAt")
    amount: MoneyResponse
    owner_share: MoneyResponse = Field(alias="ownerShare")
    obligation: ObligationResponse
