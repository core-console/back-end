"""Configuration validation and secret-handling tests."""

import pytest
from pydantic import SecretStr, ValidationError

from core_console.config import AuthMode, Settings


def test_valid_development_auth_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "development")
    monkeypatch.setenv("DEV_IDENTITY_ISSUER", "https://identity.example.test")
    monkeypatch.setenv("DEV_IDENTITY_SUBJECT", "developer")

    settings = Settings(_env_file=None, database_url=None)  # type: ignore[call-arg]

    assert settings.auth_mode is AuthMode.DEVELOPMENT
    assert settings.dev_identity_issuer == "https://identity.example.test"
    assert settings.dev_identity_subject == "developer"


def test_auth_mode_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTH_MODE", raising=False)

    with pytest.raises(ValidationError, match="AUTH_MODE"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_unsupported_auth_mode_is_rejected() -> None:
    with pytest.raises(ValidationError, match="development"):
        Settings(
            auth_mode="test",  # type: ignore[arg-type]
            dev_identity_issuer="https://identity.example.test",
            dev_identity_subject="developer",
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("dev_identity_issuer", None),
        ("dev_identity_issuer", " \t\n "),
        ("dev_identity_subject", None),
        ("dev_identity_subject", " \t\n "),
    ),
)
def test_development_identity_parts_are_required_and_not_blank(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    value: str | None,
) -> None:
    environment_name = {
        "dev_identity_issuer": "DEV_IDENTITY_ISSUER",
        "dev_identity_subject": "DEV_IDENTITY_SUBJECT",
    }[field_name]
    monkeypatch.delenv(environment_name, raising=False)

    values: dict[str, object] = {
        "auth_mode": AuthMode.DEVELOPMENT,
        "dev_identity_issuer": "https://identity.example.test",
        "dev_identity_subject": "developer",
    }
    if value is None:
        del values[field_name]
    else:
        values[field_name] = value

    with pytest.raises(ValidationError):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            database_url=None,
            **values,  # type: ignore[arg-type]
        )


def test_invalid_database_scheme_fails_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///local.db")

    with pytest.raises(ValidationError, match=r"postgresql\+psycopg"):
        Settings(
            auth_mode=AuthMode.DEVELOPMENT,
            dev_identity_issuer="https://identity.example.test",
            dev_identity_subject="developer",
        )


def test_database_url_is_not_exposed_in_repr() -> None:
    password = "not-a-real-secret"
    settings = Settings(
        auth_mode=AuthMode.DEVELOPMENT,
        dev_identity_issuer="https://identity.example.test",
        dev_identity_subject="developer",
        database_url=SecretStr(
            f"postgresql+psycopg://core_console:{password}@localhost/core_console"
        ),
    )

    assert password not in repr(settings)
