"""Finance application behavior independent of external infrastructure."""

import pytest

from core_console.modules.finance.service import (
    InvalidFinanceLedgerNameError,
    normalize_ledger_name,
)


def test_ledger_name_normalization_trims_and_uses_unicode_case_folding() -> None:
    assert normalize_ledger_name("\u2003Straße\u2003") == ("Straße", "strasse")


@pytest.mark.parametrize("name", (" \t\n ", "界" * 101))
def test_ledger_name_normalization_rejects_invalid_names(name: str) -> None:
    with pytest.raises(InvalidFinanceLedgerNameError):
        normalize_ledger_name(name)
