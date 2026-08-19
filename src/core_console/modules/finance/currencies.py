"""Deterministic backend-owned Finance currency catalog."""

from core_console.modules.finance.money import SUPPORTED_CURRENCY_MINOR_UNITS
from core_console.modules.finance.schemas import CurrencyResponse

SUPPORTED_CURRENCIES: tuple[CurrencyResponse, ...] = tuple(
    CurrencyResponse(code=code, minorUnit=minor_unit)
    for code, minor_unit in SUPPORTED_CURRENCY_MINOR_UNITS.items()
)
