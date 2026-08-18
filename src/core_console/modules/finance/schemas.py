"""Public HTTP schemas for the Finance module."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CurrencyResponse(BaseModel):
    """One backend-supported currency and its decimal scale."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    code: Literal["CNY", "JPY", "USD"]
    minor_unit: int = Field(alias="minorUnit", ge=0)


class LedgerResponse(BaseModel):
    """Closed public projection of one Finance Ledger."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    name: str


class _RequestModel(BaseModel):
    """Accept only declared public Finance JSON fields."""

    model_config = ConfigDict(
        extra="forbid",
        validate_by_alias=True,
        validate_by_name=False,
    )


class CreateLedgerRequest(_RequestModel):
    """Explicit first or additional Ledger creation."""

    name: str = Field(max_length=100)


class UpdateLedgerRequest(_RequestModel):
    """Name-only partial Ledger update."""

    name: str = Field(default_factory=str, max_length=100)
