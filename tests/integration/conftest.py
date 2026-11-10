"""Protected fixtures for real PostgreSQL integration tests."""

import ipaddress
import os
import socket
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from scripts.protected_pytest import OBSERVER
from scripts.test_postgres import prepare_postgres
from sqlalchemy import delete, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from core_console.config import AuthMode, Environment, Settings
from core_console.modules.finance.advance_correction_models import FinanceAdvanceCorrection
from core_console.modules.finance.advance_models import (
    FinanceAdvance,
    FinanceAdvanceTransaction,
    FinanceObligation,
)
from core_console.modules.finance.credit_application_models import (
    FinanceCreditApplicationAllocation,
)
from core_console.modules.finance.credit_return_models import FinanceCreditReturnFragment
from core_console.modules.finance.models import (
    FinanceAccount,
    FinanceAccountMovement,
    FinanceCategory,
    FinanceCategoryAllocation,
    FinanceLedger,
    FinanceParticipant,
    FinanceTransaction,
)
from core_console.modules.finance.settlement_correction_models import (
    FinanceSettlementCorrection,
    FinanceSettlementCorrectionTransaction,
)
from core_console.modules.finance.settlement_models import (
    FinanceCreditOrigin,
    FinanceSettlement,
    FinanceSettlementAllocation,
    FinanceSettlementIncome,
)
from core_console.modules.finance.submission_models import FinanceSubmission
from core_console.modules.users.models import User


class TestDatabaseSettings(BaseSettings):
    """Read environment-only settings for the PostgreSQL test fixtures."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    test_database_url: SecretStr | None = Field(
        default=None,
        validation_alias="TEST_DATABASE_URL",
    )
    ci: bool = Field(default=False, validation_alias="CI")
    github_actions: bool = Field(default=False, validation_alias="GITHUB_ACTIONS")
    recovery_opt_in: str = Field(default="", validation_alias="CORE_CONSOLE_TEST_POSTGRES_RECOVERY")


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
    except Exception:
        pytest.fail("TEST_DATABASE_URL is not a valid PostgreSQL URL.")
    test_database_url = test_settings.database_url_value()
    if test_database_url is None:
        pytest.fail("TEST_DATABASE_URL must contain an explicit PostgreSQL URL.")
    parsed = make_url(test_database_url)
    routing_overrides = {"host", "hostaddr", "port", "dbname", "service", "options"}
    if set(parsed.query) & routing_overrides or any(
        os.environ.get(name) for name in ("PGHOSTADDR", "PGPORT", "PGSERVICE", "PGOPTIONS")
    ):
        pytest.fail("TEST_DATABASE_URL must not use PostgreSQL routing or session overrides.")
    test_database_name = parsed.database
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
    except Exception:
        pytest.fail("development DATABASE_URL is not a valid PostgreSQL URL.")
    development_database_url = development_settings.database_url_value()
    if development_database_url is not None:
        if set(make_url(development_database_url).query) & routing_overrides:
            pytest.fail("Cannot establish separation from DATABASE_URL with routing overrides.")
        if _database_targets_overlap(
            _database_target(test_database_url),
            _database_target(development_database_url),
        ):
            pytest.fail(
                "TEST_DATABASE_URL points at the same PostgreSQL target as DATABASE_URL; "
                "a dedicated test database is required."
            )

    return test_database_url


@pytest.fixture(scope="session")
def postgres_database_url(pytestconfig: pytest.Config) -> Iterator[str]:
    """Prepare and own the dedicated database through all dependent fixture cleanup."""
    database_url = _validated_test_database_url()
    observer = pytestconfig.stash.get(OBSERVER, None)
    with prepare_postgres(
        database_url,
        Path(__file__).resolve().parents[2] / "alembic.ini",
        record=observer.postgres.append if observer is not None else lambda _event: None,
        observe_sql=observer.record_sql if observer is not None else lambda: None,
    ):
        yield database_url


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
            await session.execute(delete(FinanceSettlementCorrectionTransaction))
            await session.execute(delete(FinanceSettlementCorrection))
            await session.execute(delete(FinanceAdvanceCorrection))
            await session.execute(delete(FinanceSubmission))
            await session.execute(delete(FinanceSettlementIncome))
            await session.execute(delete(FinanceCreditApplicationAllocation))
            await session.execute(delete(FinanceCreditReturnFragment))
            await session.execute(delete(FinanceCreditOrigin))
            await session.execute(delete(FinanceSettlementAllocation))
            await session.execute(delete(FinanceSettlement))
            await session.execute(delete(FinanceAdvanceTransaction))
            await session.execute(delete(FinanceObligation))
            await session.execute(delete(FinanceAdvance))
            await session.execute(delete(FinanceCategoryAllocation))
            await session.execute(delete(FinanceAccountMovement))
            await session.execute(delete(FinanceTransaction))
            await session.execute(delete(FinanceCategory))
            await session.execute(delete(FinanceParticipant))
            await session.execute(delete(FinanceAccount))
            await session.execute(delete(FinanceLedger))
            await session.execute(delete(User))
            await session.commit()
