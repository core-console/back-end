"""Deterministic backend-owned Finance currency catalog."""

from core_console.modules.finance.schemas import CurrencyResponse

SUPPORTED_CURRENCIES: tuple[CurrencyResponse, ...] = (
    CurrencyResponse(code="CNY", minorUnit=2),
    CurrencyResponse(code="JPY", minorUnit=0),
    CurrencyResponse(code="USD", minorUnit=2),
)
