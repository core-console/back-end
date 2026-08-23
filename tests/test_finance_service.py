"""Finance application behavior independent of external infrastructure."""

from decimal import Decimal
from typing import Literal

import pytest

from core_console.modules.finance.money import InvalidMoneyError, Money
from core_console.modules.finance.service import (
    InvalidFinanceAccountNameError,
    InvalidFinanceCategoryNameError,
    InvalidFinanceLedgerNameError,
    InvalidFinanceTransactionError,
    derive_account_movement_amount,
    derive_internal_transfer_movement_amounts,
    normalize_account_name,
    normalize_category_name,
    normalize_ledger_name,
    normalize_transaction_note,
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


def test_category_name_normalization_trims_and_uses_unicode_case_folding() -> None:
    assert normalize_category_name("\u2003Straße\u2003") == ("Straße", "strasse")


@pytest.mark.parametrize("name", (" \t\n ", "界" * 101))
def test_category_name_normalization_rejects_invalid_names(name: str) -> None:
    with pytest.raises(InvalidFinanceCategoryNameError):
        normalize_category_name(name)


@pytest.mark.parametrize(
    ("kind", "nature", "expected"),
    (
        ("income", "asset", "25.00"),
        ("expense", "asset", "-25.00"),
        ("income", "liability", "-25.00"),
        ("expense", "liability", "25.00"),
    ),
)
def test_income_and_expense_derive_account_relative_movement_direction(
    kind: Literal["income", "expense"],
    nature: Literal["asset", "liability"],
    expected: str,
) -> None:
    movement = derive_account_movement_amount(
        kind=kind,
        account_nature=nature,
        economic_amount=Money.parse(amount="25.00", currency="CNY"),
    )

    assert movement.canonical_amount == expected


@pytest.mark.parametrize(
    ("source_nature", "destination_nature", "source_amount", "destination_amount"),
    [
        ("asset", "asset", Decimal("-10.00"), Decimal("10.00")),
        ("asset", "liability", Decimal("-10.00"), Decimal("-10.00")),
        ("liability", "asset", Decimal("10.00"), Decimal("10.00")),
        ("liability", "liability", Decimal("10.00"), Decimal("-10.00")),
    ],
)
def test_internal_transfer_derives_each_account_relative_movement(
    source_nature: Literal["asset", "liability"],
    destination_nature: Literal["asset", "liability"],
    source_amount: Decimal,
    destination_amount: Decimal,
) -> None:
    source, destination = derive_internal_transfer_movement_amounts(
        source_nature=source_nature,
        destination_nature=destination_nature,
        amount=Money.parse(amount="10.00", currency="CNY"),
    )

    assert source == Money(amount=source_amount, currency="CNY")
    assert destination == Money(amount=destination_amount, currency="CNY")


def test_transaction_note_trims_and_normalizes_blank_to_null() -> None:
    assert normalize_transaction_note("  plain <text>  ") == "plain <text>"
    assert normalize_transaction_note(" \t ") is None


def test_transaction_note_enforces_length_after_trimming() -> None:
    assert normalize_transaction_note(f"  {'界' * 500}  ") == "界" * 500
    with pytest.raises(InvalidFinanceTransactionError):
        normalize_transaction_note("界" * 501)
