"""Closed public schemas for deterministic Finance Transaction history."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

from core_console.modules.finance.schemas import FinanceRequestDate, FinanceTransactionResponse


def _parse_active_uncategorized(value: object) -> object:
    """Accept only the one enabled query representation from the closed contract."""

    if value == "true":
        return True
    return value


type ActiveUncategorized = Annotated[Literal[True], BeforeValidator(_parse_active_uncategorized)]


class TransactionHistoryFilters(BaseModel):
    """Validated filter and pagination input for one history request."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_date: FinanceRequestDate | None = Field(default=None, alias="fromDate")
    to_date: FinanceRequestDate | None = Field(default=None, alias="toDate")
    account_id: UUID | None = Field(default=None, alias="accountId")
    kind: Literal["income", "expense", "internalTransfer", "balanceAdjustment"] | None = None
    category_id: UUID | None = Field(default=None, alias="categoryId")
    uncategorized: ActiveUncategorized | None = None
    cursor: str | None = None
    page_size: int = Field(default=50, alias="pageSize", ge=1, le=100)

    @model_validator(mode="after")
    def validate_combinations(self) -> Self:
        """Reject closed-contract filter combinations before database work."""

        if (
            self.from_date is not None
            and self.to_date is not None
            and self.from_date > self.to_date
        ):
            raise PydanticCustomError(
                "date_range",
                "fromDate must be less than or equal to toDate.",
            )
        if self.category_id is not None and self.uncategorized is True:
            raise PydanticCustomError(
                "category_filter",
                "categoryId and uncategorized cannot be combined.",
            )
        return self


class TransactionHistoryPageResponse(BaseModel):
    """One closed cursor page of complete Finance Transactions."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    items: list[FinanceTransactionResponse]
    next_cursor: str | None = Field(alias="nextCursor")
