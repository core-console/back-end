"""Finance application behavior independent of external infrastructure."""

import pytest

from core_console.modules.finance.money import InvalidMoneyError, Money
from core_console.modules.finance.service import (
    InvalidFinanceAccountNameError,
    InvalidFinanceLedgerNameError,
    normalize_account_name,
    normalize_ledger_name,
)


def test_money_canonicalizes_amount_to_the_currency_scale() -> None:
    money = Money.parse(amount="35.1", currency="CNY")

    assert money.canonical_amount == "35.10"


def test_money_rejects_scientific_notation() -> None:
    with pytest.raises(InvalidMoneyError):
        Money.parse(amount="1e2", currency="CNY")


def test_money_rejects_precision_beyond_the_currency_scale() -> None:
    with pytest.raises(InvalidMoneyError):
        Money.parse(amount="100.1", currency="JPY")


def test_money_canonicalizes_signed_zero_without_a_negative_position() -> None:
    money = Money.parse(amount="-0", currency="USD")

    assert money.canonical_amount == "0.00"


def test_ledger_name_normalization_trims_and_uses_unicode_case_folding() -> None:
    assert normalize_ledger_name("\u2003Straße\u2003") == ("Straße", "strasse")


@pytest.mark.parametrize("name", (" \t\n ", "界" * 101))
def test_ledger_name_normalization_rejects_invalid_names(name: str) -> None:
    with pytest.raises(InvalidFinanceLedgerNameError):
        normalize_ledger_name(name)


def test_account_name_normalization_trims_and_uses_unicode_case_folding() -> None:
    assert normalize_account_name("\u2003Straße\u2003") == ("Straße", "strasse")


@pytest.mark.parametrize("name", (" \t\n ", "界" * 101))
def test_account_name_normalization_rejects_invalid_names(name: str) -> None:
    with pytest.raises(InvalidFinanceAccountNameError):
        normalize_account_name(name)
