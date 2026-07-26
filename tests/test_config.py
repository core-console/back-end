"""Configuration validation and secret-handling tests."""

import pytest
from pydantic import SecretStr, ValidationError

from core_console.config import Settings


def test_invalid_database_scheme_fails_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///local.db")

    with pytest.raises(ValidationError, match=r"postgresql\+psycopg"):
        Settings()


def test_database_url_is_not_exposed_in_repr() -> None:
    password = "not-a-real-secret"
    settings = Settings(
        database_url=SecretStr(
            f"postgresql+psycopg://core_console:{password}@localhost/core_console"
        )
    )

    assert password not in repr(settings)
