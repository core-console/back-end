"""Real PostgreSQL coverage for Finance Ledger persistence."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TypedDict
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, inspect, text
from sqlalchemy.engine.interfaces import (
    ReflectedCheckConstraint,
    ReflectedColumn,
    ReflectedForeignKeyConstraint,
    ReflectedIndex,
    ReflectedPrimaryKeyConstraint,
    ReflectedUniqueConstraint,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core_console.modules.finance.models import FinanceAccount, FinanceCategory, FinanceLedger
from core_console.modules.users.models import User

pytestmark = pytest.mark.anyio

ALEMBIC_CONFIG_PATH = Path(__file__).resolve().parents[2] / "alembic.ini"


class FinanceLedgerSchemaInspection(TypedDict):
    """Typed subset of the reflected Finance Ledger schema."""

    tables: set[str]
    columns: list[ReflectedColumn]
    primary_key: ReflectedPrimaryKeyConstraint
    foreign_keys: list[ReflectedForeignKeyConstraint]
    unique_constraints: list[ReflectedUniqueConstraint]
    check_constraints: list[ReflectedCheckConstraint]


class FinanceAccountSchemaInspection(TypedDict):
    """Typed subset of the reflected Finance Account schema."""

    columns: list[ReflectedColumn]
    primary_key: ReflectedPrimaryKeyConstraint
    foreign_keys: list[ReflectedForeignKeyConstraint]
    check_constraints: list[ReflectedCheckConstraint]


class FinanceCategorySchemaInspection(TypedDict):
    """Typed subset of the reflected Finance Category schema."""

    columns: list[ReflectedColumn]
    primary_key: ReflectedPrimaryKeyConstraint
    foreign_keys: list[ReflectedForeignKeyConstraint]
    unique_constraints: list[ReflectedUniqueConstraint]
    check_constraints: list[ReflectedCheckConstraint]
    indexes: list[ReflectedIndex]


def inspect_finance_ledgers(sync_connection: Connection) -> FinanceLedgerSchemaInspection:
    """Return the reflected schema needed by the migration contract test."""

    schema_inspector = inspect(sync_connection)
    return {
        "tables": set(schema_inspector.get_table_names()),
        "columns": schema_inspector.get_columns("finance_ledgers"),
        "primary_key": schema_inspector.get_pk_constraint("finance_ledgers"),
        "foreign_keys": schema_inspector.get_foreign_keys("finance_ledgers"),
        "unique_constraints": schema_inspector.get_unique_constraints("finance_ledgers"),
        "check_constraints": schema_inspector.get_check_constraints("finance_ledgers"),
    }


def inspect_finance_accounts(sync_connection: Connection) -> FinanceAccountSchemaInspection:
    """Return the reflected schema needed by the Account migration test."""

    schema_inspector = inspect(sync_connection)
    return {
        "columns": schema_inspector.get_columns("finance_accounts"),
        "primary_key": schema_inspector.get_pk_constraint("finance_accounts"),
        "foreign_keys": schema_inspector.get_foreign_keys("finance_accounts"),
        "check_constraints": schema_inspector.get_check_constraints("finance_accounts"),
    }


def inspect_finance_categories(sync_connection: Connection) -> FinanceCategorySchemaInspection:
    """Return the reflected schema needed by the Category migration test."""

    schema_inspector = inspect(sync_connection)
    return {
        "columns": schema_inspector.get_columns("finance_categories"),
        "primary_key": schema_inspector.get_pk_constraint("finance_categories"),
        "foreign_keys": schema_inspector.get_foreign_keys("finance_categories"),
        "unique_constraints": schema_inspector.get_unique_constraints("finance_categories"),
        "check_constraints": schema_inspector.get_check_constraints("finance_categories"),
        "indexes": schema_inspector.get_indexes("finance_categories"),
    }


async def test_migrations_create_one_owned_finance_ledger_schema(
    postgres_engine: AsyncEngine,
) -> None:
    alembic_config = Config(str(ALEMBIC_CONFIG_PATH))
    script_directory = ScriptDirectory.from_config(alembic_config)

    async with postgres_engine.connect() as connection:
        version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        schema = await connection.run_sync(inspect_finance_ledgers)

    assert script_directory.get_heads() == [script_directory.get_current_head()]
    assert version == script_directory.get_current_head()
    assert "finance_ledgers" in schema["tables"]
    columns = {column["name"]: column for column in schema["columns"]}
    assert set(columns) == {"id", "owner_id", "name", "name_key", "created_at", "updated_at"}
    assert all(column["nullable"] is False for column in columns.values())
    assert getattr(columns["name_key"]["type"], "collation", None) == "C"
    assert schema["primary_key"]["constrained_columns"] == ["id"]
    assert {
        (tuple(key["constrained_columns"]), key["referred_table"], tuple(key["referred_columns"]))
        for key in schema["foreign_keys"]
    } == {(("owner_id",), "users", ("id",))}
    assert {tuple(constraint["column_names"]) for constraint in schema["unique_constraints"]} == {
        ("owner_id", "name_key")
    }
    assert {constraint["name"] for constraint in schema["check_constraints"]} == {
        "ck_finance_ledgers_name_not_blank",
        "ck_finance_ledgers_name_trimmed",
        "ck_finance_ledgers_name_length",
        "ck_finance_ledgers_name_key_not_blank",
    }


async def test_migrations_add_account_state_without_a_stored_current_balance(
    postgres_engine: AsyncEngine,
) -> None:
    async with postgres_engine.connect() as connection:
        schema = await connection.run_sync(inspect_finance_accounts)

    columns = {column["name"]: column for column in schema["columns"]}
    assert set(columns) == {
        "id",
        "ledger_id",
        "name",
        "name_key",
        "nature",
        "currency",
        "opening_balance",
        "tracking_start_date",
        "status",
        "created_at",
        "updated_at",
    }
    assert all(column["nullable"] is False for column in columns.values())
    assert schema["primary_key"]["constrained_columns"] == ["id"]
    assert {
        (tuple(key["constrained_columns"]), key["referred_table"], tuple(key["referred_columns"]))
        for key in schema["foreign_keys"]
    } == {(("ledger_id",), "finance_ledgers", ("id",))}
    assert {constraint["name"] for constraint in schema["check_constraints"]} == {
        "ck_finance_accounts_name_not_blank",
        "ck_finance_accounts_name_trimmed",
        "ck_finance_accounts_name_length",
        "ck_finance_accounts_name_key_not_blank",
        "ck_finance_accounts_nature",
        "ck_finance_accounts_currency",
        "ck_finance_accounts_opening_balance_finite",
        "ck_finance_accounts_opening_balance_scale",
        "ck_finance_accounts_status",
    }


async def test_migrations_add_only_durable_ledger_owned_category_state(
    postgres_engine: AsyncEngine,
) -> None:
    async with postgres_engine.connect() as connection:
        schema = await connection.run_sync(inspect_finance_categories)

    columns = {column["name"]: column for column in schema["columns"]}
    assert set(columns) == {
        "id",
        "ledger_id",
        "name",
        "name_key",
        "status",
        "created_at",
        "updated_at",
    }
    assert all(column["nullable"] is False for column in columns.values())
    assert getattr(columns["name_key"]["type"], "collation", None) == "C"
    assert schema["primary_key"]["constrained_columns"] == ["id"]
    assert {
        (tuple(key["constrained_columns"]), key["referred_table"], tuple(key["referred_columns"]))
        for key in schema["foreign_keys"]
    } == {(("ledger_id",), "finance_ledgers", ("id",))}
    assert {tuple(constraint["column_names"]) for constraint in schema["unique_constraints"]} == {
        ("ledger_id", "name_key")
    }
    assert {constraint["name"] for constraint in schema["check_constraints"]} == {
        "ck_finance_categories_name_not_blank",
        "ck_finance_categories_name_trimmed",
        "ck_finance_categories_name_length",
        "ck_finance_categories_name_key_not_blank",
        "ck_finance_categories_status",
    }
    assert {(index["name"], tuple(index["column_names"])) for index in schema["indexes"]} >= {
        (
            "ix_finance_categories_ledger_status_name_key_id",
            ("ledger_id", "status", "name_key", "id"),
        )
    }


async def test_finance_ledger_requires_a_real_owner(
    postgres_session: AsyncSession,
) -> None:
    postgres_session.add(
        FinanceLedger(
            owner_id=uuid4(),
            name="Personal",
            name_key="personal",
        )
    )

    with pytest.raises(IntegrityError):
        await postgres_session.commit()


async def test_finance_ledger_name_key_is_unique_per_owner(
    postgres_session: AsyncSession,
) -> None:
    first_owner = _user("first-owner")
    second_owner = _user("second-owner")
    postgres_session.add_all([first_owner, second_owner])
    await postgres_session.flush()
    postgres_session.add_all(
        [
            _ledger(first_owner.id, name="Personal", name_key="personal"),
            _ledger(second_owner.id, name="PERSONAL", name_key="personal"),
        ]
    )
    await postgres_session.commit()

    postgres_session.add(_ledger(first_owner.id, name="PERSONAL", name_key="personal"))
    with pytest.raises(IntegrityError):
        await postgres_session.commit()


@pytest.mark.parametrize(
    ("name", "name_key"),
    (("", "empty"), (" Personal ", "personal"), ("x" * 101, "long"), ("Valid", "")),
)
async def test_finance_ledger_rejects_invalid_persisted_names(
    postgres_session: AsyncSession,
    name: str,
    name_key: str,
) -> None:
    owner = _user(uuid4().hex)
    postgres_session.add(owner)
    await postgres_session.flush()
    postgres_session.add(_ledger(owner.id, name=name, name_key=name_key))

    with pytest.raises(IntegrityError):
        await postgres_session.commit()


@pytest.mark.parametrize(
    ("nature", "currency", "opening_balance", "status"),
    (
        ("equity", "CNY", Decimal("0.00"), "active"),
        ("asset", "EUR", Decimal("0.00"), "active"),
        ("asset", "JPY", Decimal("0.1"), "active"),
        ("asset", "JPY", Decimal("100.0"), "active"),
        ("asset", "CNY", Decimal("1.000"), "active"),
        ("liability", "USD", Decimal("1.001"), "active"),
        ("asset", "CNY", Decimal("NaN"), "active"),
        ("asset", "CNY", Decimal("Infinity"), "active"),
        ("asset", "CNY", Decimal("-Infinity"), "active"),
        ("asset", "CNY", Decimal("0.00"), "deleted"),
    ),
)
async def test_finance_account_rejects_invalid_persisted_semantics(
    postgres_session: AsyncSession,
    nature: str,
    currency: str,
    opening_balance: Decimal,
    status: str,
) -> None:
    owner = _user(uuid4().hex)
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    postgres_session.add(
        _account(
            ledger.id,
            nature=nature,
            currency=currency,
            opening_balance=opening_balance,
            status=status,
        )
    )

    with pytest.raises(IntegrityError):
        await postgres_session.commit()


async def test_finance_account_accepts_supported_opening_balance_scale_boundaries(
    postgres_session: AsyncSession,
) -> None:
    owner = _user("valid-scale-boundaries")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    postgres_session.add_all(
        [
            _account(
                ledger.id,
                name="Yen",
                name_key="yen",
                nature="asset",
                currency="JPY",
                opening_balance=Decimal("100"),
                status="active",
            ),
            _account(
                ledger.id,
                name="Yuan",
                name_key="yuan",
                nature="asset",
                currency="CNY",
                opening_balance=Decimal("1.00"),
                status="active",
            ),
            _account(
                ledger.id,
                name="Dollar",
                name_key="dollar",
                nature="liability",
                currency="USD",
                opening_balance=Decimal("-1.00"),
                status="active",
            ),
        ]
    )

    await postgres_session.commit()


@pytest.mark.parametrize(
    ("name", "name_key"),
    (("", "empty"), (" Account ", "account"), ("x" * 101, "long"), ("Valid", "")),
)
async def test_finance_account_rejects_invalid_persisted_names(
    postgres_session: AsyncSession,
    name: str,
    name_key: str,
) -> None:
    owner = _user(uuid4().hex)
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    postgres_session.add(
        _account(
            ledger.id,
            name=name,
            name_key=name_key,
            nature="asset",
            currency="CNY",
            opening_balance=Decimal("0.00"),
            status="active",
        )
    )

    with pytest.raises(IntegrityError):
        await postgres_session.commit()


async def test_finance_category_name_key_is_unique_per_ledger_including_archived(
    postgres_session: AsyncSession,
) -> None:
    owner = _user("category-unique-owner")
    postgres_session.add(owner)
    await postgres_session.flush()
    first_ledger = _ledger(owner.id, name="First", name_key="first")
    second_ledger = _ledger(owner.id, name="Second", name_key="second")
    postgres_session.add_all([first_ledger, second_ledger])
    await postgres_session.flush()
    postgres_session.add_all(
        [
            _category(
                first_ledger.id,
                name="Straße",
                name_key="strasse",
                status="archived",
            ),
            _category(
                second_ledger.id,
                name="STRASSE",
                name_key="strasse",
                status="active",
            ),
        ]
    )
    await postgres_session.commit()

    postgres_session.add(
        _category(
            first_ledger.id,
            name="STRASSE",
            name_key="strasse",
            status="active",
        )
    )
    with pytest.raises(IntegrityError):
        await postgres_session.commit()


@pytest.mark.parametrize(
    ("name", "name_key", "status"),
    (
        ("", "empty", "active"),
        (" Category ", "category", "active"),
        ("x" * 101, "long", "active"),
        ("Valid", "", "active"),
        ("Valid", "valid", "deleted"),
    ),
)
async def test_finance_category_rejects_invalid_persisted_state(
    postgres_session: AsyncSession,
    name: str,
    name_key: str,
    status: str,
) -> None:
    owner = _user(uuid4().hex)
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    postgres_session.add(_category(ledger.id, name=name, name_key=name_key, status=status))

    with pytest.raises(IntegrityError):
        await postgres_session.commit()


def _user(subject: str) -> User:
    return User(
        identity_issuer="https://identity.example.test/finance",
        identity_subject=subject,
        status="active",
    )


def _ledger(owner_id: UUID, *, name: str, name_key: str) -> FinanceLedger:
    return FinanceLedger(owner_id=owner_id, name=name, name_key=name_key)


def _account(
    ledger_id: UUID,
    *,
    name: str = "Account",
    name_key: str = "account",
    nature: str,
    currency: str,
    opening_balance: Decimal,
    status: str,
) -> FinanceAccount:
    return FinanceAccount(
        ledger_id=ledger_id,
        name=name,
        name_key=name_key,
        nature=nature,
        currency=currency,
        opening_balance=opening_balance,
        tracking_start_date=date(2026, 8, 1),
        status=status,
    )


def _category(
    ledger_id: UUID,
    *,
    name: str,
    name_key: str,
    status: str,
) -> FinanceCategory:
    return FinanceCategory(
        ledger_id=ledger_id,
        name=name,
        name_key=name_key,
        status=status,
    )
