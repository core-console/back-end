"""Exact currency-aware Money values for Finance workflows."""

from dataclasses import dataclass
from decimal import Decimal
from re import fullmatch
from typing import Literal

type CurrencyCode = Literal["CNY", "JPY", "USD"]

SUPPORTED_CURRENCY_MINOR_UNITS: dict[CurrencyCode, int] = {
    "CNY": 2,
    "JPY": 0,
    "USD": 2,
}


class InvalidMoneyError(ValueError):
    """A Money value violates the exact public Finance contract."""


@dataclass(frozen=True, slots=True)
class Money:
    """An exact amount paired with one supported currency."""

    amount: Decimal
    currency: CurrencyCode

    @classmethod
    def parse(cls, *, amount: str, currency: CurrencyCode) -> Money:
        """Parse one exact decimal-string Money value."""

        if fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", amount) is None:
            raise InvalidMoneyError("Money amount must be a plain base-10 decimal string.")
        fractional_digits = len(amount.partition(".")[2])
        if fractional_digits > SUPPORTED_CURRENCY_MINOR_UNITS[currency]:
            raise InvalidMoneyError("Money amount exceeds the currency's supported precision.")
        return cls(amount=Decimal(amount), currency=currency)

    @property
    def canonical_amount(self) -> str:
        """Return the amount at the supported currency's catalog scale."""

        scale = SUPPORTED_CURRENCY_MINOR_UNITS[self.currency]
        canonical_value = Decimal(0) if self.amount == 0 else self.amount
        return f"{canonical_value:.{scale}f}"
