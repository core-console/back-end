"""Protected fixtures for real PostgreSQL integration tests."""

import asyncio
import ipaddress
import platform
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Connection, delete, make_url, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from core_console.config import AuthMode, Environment, Settings
from core_console.modules.finance.models import FinanceAccount, FinanceCategory, FinanceLedger
from core_console.modules.users.models import User


class TestDatabaseSettings(BaseSettings):
    """Read only the explicit test database environment variable."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    test_database_url: SecretStr | None = Field(
        default=None,
        validation_alias="TEST_DATABASE_URL",
    )
    ci: bool = Field(default=False, validation_alias="CI")
    github_actions: bool = Field(default=False, validation_alias="GITHUB_ACTIONS")


type DatabaseTarget = tuple[frozenset[str], int, str | None]


def _resolved_hosts(host: str) -> frozenset[str]:
    """Resolve a host to addresses so aliases cannot bypass the safety check."""

    try:
        addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        pytest.fail(f"Could not resolve database host {host!r}: {exc}")
    return frozenset(_canonical_address(str(address[4][0])) for address in addresses)


def _canonical_address(address: str) -> str:
    """Normalize loopback addresses so DNS aliases cannot evade the guard."""

    try:
        parsed_address = ipaddress.ip_address(address)
    except ValueError:
        return address.casefold()
    if isinstance(parsed_address, ipaddress.IPv6Address) and parsed_address.ipv4_mapped:
        parsed_address = parsed_address.ipv4_mapped
    if parsed_address.is_loopback:
        return "<local>"
    return str(parsed_address)


def _database_target(database_url: str) -> DatabaseTarget:
    """Return the server/database identity without exposing credentials."""

    parsed_url = make_url(database_url)
    host = parsed_url.host.casefold() if parsed_url.host is not None else None
    if host in {"localhost", "127.0.0.1", "::1"}:
        resolved_hosts = frozenset({"<local>"})
    elif host is None:
        resolved_hosts = frozenset({"<unix>"})
    else:
        resolved_hosts = _resolved_hosts(host)
    return (
        resolved_hosts,
        parsed_url.port or 5432,
        parsed_url.database.casefold() if parsed_url.database is not None else None,
    )


def _database_targets_overlap(left: DatabaseTarget, right: DatabaseTarget) -> bool:
    """Treat any shared address as a possible shared PostgreSQL target."""

    return left[1] == right[1] and left[2] == right[2] and bool(left[0] & right[0])


def _validated_test_database_url() -> str:
    """Require an explicit, non-development PostgreSQL test target."""

    test_environment = TestDatabaseSettings()
    if test_environment.test_database_url is None:
        if test_environment.ci or test_environment.github_actions:
            pytest.fail("TEST_DATABASE_URL is required in CI for PostgreSQL integration tests.")
        pytest.skip("TEST_DATABASE_URL is not set; PostgreSQL integration tests were not run.")

    try:
        test_settings = Settings(
            environment=Environment.TEST,
            auth_mode=AuthMode.DEVELOPMENT,
            dev_identity_issuer="https://identity.example.test",
            dev_identity_subject="test-developer",
            database_url=test_environment.test_database_url,
        )
    except Exception as exc:
        pytest.fail(f"TEST_DATABASE_URL is not a valid PostgreSQL URL: {exc}")
    test_database_url = test_settings.database_url_value()
    if test_database_url is None:
        pytest.fail("TEST_DATABASE_URL must contain an explicit PostgreSQL URL.")
    test_database_name = make_url(test_database_url).database
    if test_database_name is None or "test" not in test_database_name.casefold().replace(
        "-", "_"
    ).split("_"):
        pytest.fail(
            "TEST_DATABASE_URL must name a dedicated test database, such as core_console_test."
        )

    try:
        development_settings = Settings(
            environment=Environment.DEVELOPMENT,
            auth_mode=AuthMode.DEVELOPMENT,
            dev_identity_issuer="https://identity.example.test",
            dev_identity_subject="test-developer",
        )
    except Exception as exc:
        pytest.fail(f"development DATABASE_URL is not a valid PostgreSQL URL: {exc}")
    development_database_url = development_settings.database_url_value()
    if development_database_url is not None and _database_targets_overlap(
        _database_target(test_database_url),
        _database_target(development_database_url),
    ):
        pytest.fail(
            "TEST_DATABASE_URL points at the same PostgreSQL target as DATABASE_URL; "
            "a dedicated test database is required."
        )

    return test_database_url


async def _reset_and_upgrade(database_url: str) -> None:
    """Reset the protected test target, then apply every migration."""

    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA public CASCADE"))
            await connection.execute(text("CREATE SCHEMA public"))

        async with engine.begin() as connection:

            def upgrade(sync_connection: Connection) -> None:
                config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
                config.attributes["connection"] = sync_connection
                command.upgrade(config, "head")

            await connection.run_sync(upgrade)
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def postgres_database_url() -> str:
    """Prepare a dedicated TEST_DATABASE_URL and never fall back to DATABASE_URL."""

    database_url = _validated_test_database_url()
    if platform.system() == "Windows":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(_reset_and_upgrade(database_url))
    else:
        asyncio.run(_reset_and_upgrade(database_url))
    return database_url


@pytest.fixture
async def postgres_engine(postgres_database_url: str) -> AsyncIterator[AsyncEngine]:
    """Yield an async engine connected only to the protected test target."""

    engine = create_async_engine(postgres_database_url, pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def postgres_session(
    postgres_engine: AsyncEngine,
) -> AsyncIterator[AsyncSession]:
    """Yield a session and clean only users in the protected test database."""

    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with session_factory() as session:
        try:
            yield session
        finally:
            await session.rollback()
            await session.execute(delete(FinanceCategory))
            await session.execute(delete(FinanceAccount))
            await session.execute(delete(FinanceLedger))
            await session.execute(delete(User))
            await session.commit()
