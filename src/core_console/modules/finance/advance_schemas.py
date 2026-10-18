"""Versioned Advance intent and confirmed source projections."""

from datetime import date, datetime
from decimal import Decimal
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core_console.modules.finance.money import subtract_money_amounts_exact
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


class ParticipantShareV2(BaseModel):
    model_config = ConfigDict(extra="forbid")
    participant_id: UUID = Field(alias="participantId")
    amount: OpeningBalanceV1

    def canonical(self) -> dict[str, object]:
        return {"participantId": str(self.participant_id), "amount": self.amount.canonical()}


class AdvanceDraftV2(_TransactionCommandV1):
    funding_account_id: UUID = Field(alias="fundingAccountId")
    amount: OpeningBalanceV1
    owner_share: OpeningBalanceV1 = Field(alias="ownerShare")
    participant_shares: list[ParticipantShareV2] = Field(alias="participantShares", min_length=1)

    @model_validator(mode="after")
    def shared_purchase(self) -> Self:
        if Decimal(self.amount.amount) <= 0 or Decimal(self.owner_share.amount) < 0:
            raise ValueError("Funding must be positive and owner share non-negative.")
        if self.owner_share.currency != self.amount.currency:
            raise ValueError("Funding and shares must use one currency.")
        remaining = subtract_money_amounts_exact(
            Decimal(self.amount.amount), Decimal(self.owner_share.amount)
        )
        participants: set[UUID] = set()
        for share in self.participant_shares:
            if share.participant_id in participants:
                raise ValueError("Each Participant must have exactly one share.")
            participants.add(share.participant_id)
            if share.amount.currency != self.amount.currency or Decimal(share.amount.amount) <= 0:
                raise ValueError(
                    "Participant shares must be positive and use the funding currency."
                )
            remaining = subtract_money_amounts_exact(remaining, Decimal(share.amount.amount))
        if remaining != 0:
            raise ValueError("Owner and participant shares must equal the funded total exactly.")
        return self

    def canonical(self) -> dict[str, object]:
        return {
            "fundingAccountId": str(self.funding_account_id),
            "transactionDate": self.transaction_date.isoformat(),
            "amount": self.amount.canonical(),
            "ownerShare": self.owner_share.canonical(),
            "participantShares": [
                share.canonical()
                for share in sorted(self.participant_shares, key=lambda share: share.participant_id)
            ],
            "note": self.note,
        }


class AdvanceCommandV2(AdvanceDraftV2):
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


class ParticipantReceivablePreview(_Projection):
    participant_id: UUID = Field(alias="participantId")
    principal: MoneyResponse


class SharedAdvancePreview(AdvancePreview):
    participant_receivables: list[ParticipantReceivablePreview] = Field(
        alias="participantReceivables"
    )


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


class _AdvanceSource(_Projection):
    id: UUID
    ledger_id: UUID = Field(alias="ledgerId")
    funding_account_id: UUID = Field(alias="fundingAccountId")
    transaction_date: date = Field(alias="transactionDate")
    note: str | None
    confirmed_at: datetime = Field(alias="confirmedAt")
    amount: MoneyResponse
    owner_share: MoneyResponse = Field(alias="ownerShare")


class AdvanceResponse(_AdvanceSource):
    transaction_id: UUID = Field(alias="transactionId")
    obligation: ObligationResponse


class SharedAdvanceResponse(_AdvanceSource):
    transaction_ids: list[UUID] = Field(alias="transactionIds")
    obligations: list[ObligationResponse]
