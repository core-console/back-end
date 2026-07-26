"""Validated application configuration."""

from enum import StrEnum
from typing import Annotated

from pydantic import AnyUrl, Field, SecretStr, TypeAdapter, UrlConstraints, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DatabaseUrl = Annotated[
    AnyUrl,
    UrlConstraints(allowed_schemes=["postgresql+psycopg"], host_required=True),
]
_DATABASE_URL_ADAPTER = TypeAdapter(DatabaseUrl)


class Environment(StrEnum):
    """Supported runtime modes."""

    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """Application settings loaded from initialization values and the environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    environment: Environment = Field(
        default=Environment.DEVELOPMENT,
        validation_alias="APP_ENV",
    )
    database_url: SecretStr | None = Field(
        default=None,
        validation_alias="DATABASE_URL",
        repr=False,
    )
    database_connect_timeout_seconds: float = Field(
        default=2.0,
        ge=0.1,
        le=30.0,
        validation_alias="DATABASE_CONNECT_TIMEOUT_SECONDS",
    )

    @field_validator("database_url", mode="before")
    @classmethod
    def validate_database_url(cls, value: object) -> object:
        """Accept an omitted URL but require the explicit async psycopg dialect."""

        if value is None or value == "":
            return None

        raw_value = value.get_secret_value() if isinstance(value, SecretStr) else value
        _DATABASE_URL_ADAPTER.validate_python(raw_value)
        return value

    def database_url_value(self) -> str | None:
        """Reveal the URL only at the infrastructure boundary that needs it."""

        if self.database_url is None:
            return None
        return self.database_url.get_secret_value()


def load_settings() -> Settings:
    """Load and validate settings without caching mutable process state."""

    return Settings()
