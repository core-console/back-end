"""Exact currency-aware Money values for Finance workflows."""

from dataclasses import dataclass
from decimal import (
    MAX_EMAX,
    MAX_PREC,
    MIN_EMIN,
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    Inexact,
    Overflow,
)
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


def subtract_money_amounts_exact(minuend: Decimal, subtrahend: Decimal) -> Decimal:
    """Subtract finite Money amounts without depending on ambient precision."""

    if not minuend.is_finite() or not subtrahend.is_finite():
        raise InvalidMoneyError("Money subtraction requires finite amounts.")
    minuend_tuple = minuend.as_tuple()
    subtrahend_tuple = subtrahend.as_tuple()
    minuend_exponent = int(minuend_tuple.exponent)
    subtrahend_exponent = int(subtrahend_tuple.exponent)
    common_exponent = min(minuend_exponent, subtrahend_exponent)
    minuend_digits = len(minuend_tuple.digits) + minuend_exponent - common_exponent
    subtrahend_digits = len(subtrahend_tuple.digits) + subtrahend_exponent - common_exponent
    aligned_precision = max(minuend_digits, subtrahend_digits) + 1
    etiny_precision = max(1, MIN_EMIN - common_exponent + 1)
    context = Context(
        prec=min(MAX_PREC, max(aligned_precision, etiny_precision)),
        rounding=ROUND_HALF_EVEN,
        Emin=MIN_EMIN,
        Emax=MAX_EMAX,
        capitals=1,
        clamp=0,
        traps=[Inexact, Overflow],
    )
    return context.subtract(minuend, subtrahend)
