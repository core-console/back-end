"""Real PostgreSQL coverage for Finance Ledger persistence."""

import asyncio
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TypedDict
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, func, inspect, select, text
from sqlalchemy.engine.interfaces import (
    ReflectedCheckConstraint,
    ReflectedColumn,
    ReflectedForeignKeyConstraint,
    ReflectedIndex,
    ReflectedPrimaryKeyConstraint,
    ReflectedUniqueConstraint,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from core_console.modules.finance.models import (
    FinanceAccount,
    FinanceAccountMovement,
    FinanceCategory,
    FinanceCategoryAllocation,
    FinanceLedger,
    FinanceTransaction,
)
from core_console.modules.finance.money import Money
from core_console.modules.finance.queries import FinanceTransactionDetail
from core_console.modules.finance.service import create_finance_transaction
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


class FinanceTransactionSchemaInspection(TypedDict):
    """Reflected durable Transaction table shape and named invariants."""

    tables: set[str]
    transaction_columns: set[str]
    movement_columns: set[str]
    allocation_columns: set[str]
    transaction_checks: set[str | None]
    movement_checks: set[str | None]
    allocation_checks: set[str | None]
    movement_foreign_keys: set[str | None]
    allocation_foreign_keys: set[str | None]
    transaction_uniques: set[str | None]
    movement_uniques: set[str | None]
    allocation_uniques: set[str | None]
    account_uniques: set[str | None]
    category_uniques: set[str | None]
    constraint_triggers: set[str]


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


def inspect_finance_transactions(
    sync_connection: Connection,
) -> FinanceTransactionSchemaInspection:
    """Return the first durable Transaction schema and integrity constraints."""

    schema_inspector = inspect(sync_connection)
    return {
        "tables": set(schema_inspector.get_table_names()),
        "transaction_columns": {
            column["name"] for column in schema_inspector.get_columns("finance_transactions")
        },
        "movement_columns": {
            column["name"] for column in schema_inspector.get_columns("finance_account_movements")
        },
        "allocation_columns": {
            column["name"]
            for column in schema_inspector.get_columns("finance_category_allocations")
        },
        "transaction_checks": {
            constraint["name"]
            for constraint in schema_inspector.get_check_constraints("finance_transactions")
        },
        "movement_checks": {
            constraint["name"]
            for constraint in schema_inspector.get_check_constraints("finance_account_movements")
        },
        "allocation_checks": {
            constraint["name"]
            for constraint in schema_inspector.get_check_constraints("finance_category_allocations")
        },
        "movement_foreign_keys": {
            constraint["name"]
            for constraint in schema_inspector.get_foreign_keys("finance_account_movements")
        },
        "allocation_foreign_keys": {
            constraint["name"]
            for constraint in schema_inspector.get_foreign_keys("finance_category_allocations")
        },
        "transaction_uniques": {
            constraint["name"]
            for constraint in schema_inspector.get_unique_constraints("finance_transactions")
        },
        "movement_uniques": {
            constraint["name"]
            for constraint in schema_inspector.get_unique_constraints("finance_account_movements")
        },
        "allocation_uniques": {
            constraint["name"]
            for constraint in schema_inspector.get_unique_constraints(
                "finance_category_allocations"
            )
        },
        "account_uniques": {
            constraint["name"]
            for constraint in schema_inspector.get_unique_constraints("finance_accounts")
        },
        "category_uniques": {
            constraint["name"]
            for constraint in schema_inspector.get_unique_constraints("finance_categories")
        },
        "constraint_triggers": set(
            sync_connection.execute(
                text(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE NOT tgisinternal AND tgrelid IN ("
                    "'finance_accounts'::regclass, "
                    "'finance_transactions'::regclass, "
                    "'finance_account_movements'::regclass, "
                    "'finance_category_allocations'::regclass)"
                )
            ).scalars()
        ),
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
        ("ledger_id", "name_key"),
        ("id", "ledger_id"),
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


async def test_migration_adds_atomic_transaction_movement_and_allocation_integrity(
    postgres_engine: AsyncEngine,
) -> None:
    async with postgres_engine.connect() as connection:
        schema = await connection.run_sync(inspect_finance_transactions)

    assert {
        "finance_transactions",
        "finance_account_movements",
        "finance_category_allocations",
    } <= schema["tables"]
    assert schema["transaction_columns"] == {
        "id",
        "ledger_id",
        "kind",
        "transaction_date",
        "note",
        "created_at",
        "updated_at",
    }
    assert schema["movement_columns"] == {
        "id",
        "transaction_id",
        "ledger_id",
        "account_id",
        "amount",
        "currency",
    }
    assert schema["allocation_columns"] == {
        "id",
        "transaction_id",
        "ledger_id",
        "category_id",
        "amount",
        "currency",
    }
    assert schema["transaction_checks"] == {
        "ck_finance_transactions_kind",
        "ck_finance_transactions_note",
    }
    assert schema["movement_checks"] == {
        "ck_finance_account_movements_amount_finite",
        "ck_finance_account_movements_amount_nonzero",
        "ck_finance_account_movements_currency",
        "ck_finance_account_movements_amount_scale",
    }
    assert schema["allocation_checks"] == {
        "ck_finance_category_allocations_amount_finite",
        "ck_finance_category_allocations_amount_positive",
        "ck_finance_category_allocations_currency",
        "ck_finance_category_allocations_amount_scale",
    }
    assert schema["movement_foreign_keys"] == {
        "fk_finance_account_movements_transaction_ledger",
        "fk_finance_account_movements_account_ledger",
    }
    assert schema["allocation_foreign_keys"] == {
        "fk_finance_category_allocations_transaction_ledger",
        "fk_finance_category_allocations_category_ledger",
    }
    assert schema["transaction_uniques"] == {"uq_finance_transactions_id_ledger_id"}
    assert schema["movement_uniques"] == set()
    assert schema["allocation_uniques"] == {"uq_finance_category_allocations_transaction_id"}
    assert "uq_finance_accounts_id_ledger_id" in schema["account_uniques"]
    assert "uq_finance_categories_id_ledger_id" in schema["category_uniques"]
    assert schema["constraint_triggers"] == {
        "ck_finance_accounts_semantics_lock",
        "ck_finance_accounts_tracking_start_integrity",
        "ck_finance_transactions_ordinary_integrity",
        "ck_finance_account_movements_ordinary_integrity",
        "ck_finance_category_allocations_ordinary_integrity",
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


async def test_transaction_projection_failure_rolls_back_complete_atomic_write(
    postgres_session: AsyncSession,
) -> None:
    owner = _user("transaction-projection-failure")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add(account)
    await postgres_session.commit()

    def fail_projection(_: FinanceTransactionDetail) -> None:
        raise RuntimeError("response projection failed")

    with pytest.raises(RuntimeError, match="response projection failed"):
        await create_finance_transaction(
            postgres_session,
            owner_id=owner.id,
            ledger_id=ledger.id,
            kind="income",
            account_id=account.id,
            transaction_date=date(2026, 8, 21),
            economic_amount=Money.parse(amount="10.00", currency="CNY"),
            allocation_amount=Money.parse(amount="10.00", currency="CNY"),
            category_id=None,
            note=None,
            project=fail_projection,
        )

    counts = [
        await postgres_session.scalar(select(func.count()).select_from(model))
        for model in (
            FinanceTransaction,
            FinanceAccountMovement,
            FinanceCategoryAllocation,
        )
    ]
    assert counts == [0, 0, 0]


async def test_database_rejects_an_incomplete_income_transaction(
    postgres_session: AsyncSession,
) -> None:
    owner = _user("incomplete-transaction")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    postgres_session.add(
        FinanceTransaction(
            ledger_id=ledger.id,
            kind="income",
            transaction_date=date(2026, 8, 21),
            note=None,
        )
    )

    with pytest.raises(IntegrityError):
        await postgres_session.commit()


async def test_database_rejects_incoherent_income_movement_direction(
    postgres_session: AsyncSession,
) -> None:
    owner = _user("incoherent-income")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add(account)
    await postgres_session.flush()
    transaction = FinanceTransaction(
        ledger_id=ledger.id,
        kind="income",
        transaction_date=date(2026, 8, 21),
        note=None,
    )
    postgres_session.add(transaction)
    await postgres_session.flush()
    postgres_session.add_all(
        [
            FinanceAccountMovement(
                transaction_id=transaction.id,
                ledger_id=ledger.id,
                account_id=account.id,
                amount=Decimal("-10.00"),
                currency="CNY",
            ),
            FinanceCategoryAllocation(
                transaction_id=transaction.id,
                ledger_id=ledger.id,
                category_id=None,
                amount=Decimal("10.00"),
                currency="CNY",
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        await postgres_session.commit()


async def test_database_rejects_movement_currency_that_differs_from_account(
    postgres_session: AsyncSession,
) -> None:
    owner = _user("cross-currency-movement")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add(account)
    await postgres_session.flush()
    transaction = FinanceTransaction(
        ledger_id=ledger.id,
        kind="income",
        transaction_date=date(2026, 8, 21),
        note=None,
    )
    postgres_session.add(transaction)
    await postgres_session.flush()
    postgres_session.add_all(
        [
            FinanceAccountMovement(
                transaction_id=transaction.id,
                ledger_id=ledger.id,
                account_id=account.id,
                amount=Decimal("10.00"),
                currency="USD",
            ),
            FinanceCategoryAllocation(
                transaction_id=transaction.id,
                ledger_id=ledger.id,
                category_id=None,
                amount=Decimal("10.00"),
                currency="USD",
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        await postgres_session.commit()


async def test_database_rejects_transaction_before_account_tracking_start(
    postgres_session: AsyncSession,
) -> None:
    owner = _user("transaction-before-tracking")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add(account)
    await postgres_session.flush()
    transaction = FinanceTransaction(
        ledger_id=ledger.id,
        kind="income",
        transaction_date=date(2026, 7, 31),
        note=None,
    )
    postgres_session.add(transaction)
    await postgres_session.flush()
    postgres_session.add_all(
        [
            FinanceAccountMovement(
                transaction_id=transaction.id,
                ledger_id=ledger.id,
                account_id=account.id,
                amount=Decimal("10.00"),
                currency="CNY",
            ),
            FinanceCategoryAllocation(
                transaction_id=transaction.id,
                ledger_id=ledger.id,
                category_id=None,
                amount=Decimal("10.00"),
                currency="CNY",
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        await postgres_session.commit()


async def test_database_rejects_reparenting_an_account_movement(
    postgres_session: AsyncSession,
) -> None:
    owner = _user("movement-reparenting")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add(account)
    await postgres_session.commit()

    def transaction_id(detail: FinanceTransactionDetail) -> UUID:
        return detail.transaction.id

    first_transaction_id = await create_finance_transaction(
        postgres_session,
        owner_id=owner.id,
        ledger_id=ledger.id,
        kind="income",
        account_id=account.id,
        transaction_date=date(2026, 8, 21),
        economic_amount=Money.parse(amount="10.00", currency="CNY"),
        allocation_amount=Money.parse(amount="10.00", currency="CNY"),
        category_id=None,
        note=None,
        project=transaction_id,
    )
    movement = await postgres_session.scalar(
        select(FinanceAccountMovement).where(
            FinanceAccountMovement.transaction_id == first_transaction_id
        )
    )
    assert movement is not None
    second_transaction = FinanceTransaction(
        ledger_id=ledger.id,
        kind="income",
        transaction_date=date(2026, 8, 22),
        note=None,
    )
    postgres_session.add(second_transaction)
    await postgres_session.flush()
    postgres_session.add(
        FinanceCategoryAllocation(
            transaction_id=second_transaction.id,
            ledger_id=ledger.id,
            category_id=None,
            amount=Decimal("10.00"),
            currency="CNY",
        )
    )
    movement.transaction_id = second_transaction.id

    with pytest.raises(IntegrityError):
        await postgres_session.commit()


async def test_database_rejects_tracking_start_edit_that_excludes_history(
    postgres_session: AsyncSession,
) -> None:
    owner = _user("tracking-start-persistence")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add(account)
    await postgres_session.commit()
    await create_finance_transaction(
        postgres_session,
        owner_id=owner.id,
        ledger_id=ledger.id,
        kind="income",
        account_id=account.id,
        transaction_date=date(2026, 8, 21),
        economic_amount=Money.parse(amount="10.00", currency="CNY"),
        allocation_amount=Money.parse(amount="10.00", currency="CNY"),
        category_id=None,
        note=None,
        project=lambda detail: detail.transaction.id,
    )
    account.tracking_start_date = date(2026, 8, 22)

    with pytest.raises(IntegrityError):
        await postgres_session.commit()


@pytest.mark.parametrize("semantic_change", ("nature", "currency"))
async def test_database_rejects_account_semantic_change_with_durable_history(
    postgres_session: AsyncSession,
    semantic_change: str,
) -> None:
    owner = _user(f"account-history-{semantic_change}")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add(account)
    await postgres_session.commit()
    await create_finance_transaction(
        postgres_session,
        owner_id=owner.id,
        ledger_id=ledger.id,
        kind="income",
        account_id=account.id,
        transaction_date=date(2026, 8, 21),
        economic_amount=Money.parse(amount="10.00", currency="CNY"),
        allocation_amount=Money.parse(amount="10.00", currency="CNY"),
        category_id=None,
        note=None,
        project=lambda detail: detail.transaction.id,
    )

    if semantic_change == "nature":
        account.nature = "liability"
    else:
        account.currency = "USD"

    with pytest.raises(IntegrityError):
        await postgres_session.commit()


@pytest.mark.parametrize("semantic_change", ("nature", "currency"))
async def test_database_rejects_account_semantic_change_with_opening_balance(
    postgres_session: AsyncSession,
    semantic_change: str,
) -> None:
    owner = _user(f"account-opening-{semantic_change}")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("25.00"),
        status="active",
    )
    postgres_session.add(account)
    await postgres_session.commit()

    if semantic_change == "nature":
        account.nature = "liability"
    else:
        account.currency = "USD"

    with pytest.raises(IntegrityError):
        await postgres_session.commit()


async def test_database_allows_account_semantic_change_while_unlocked(
    postgres_session: AsyncSession,
) -> None:
    owner = _user("account-semantics-unlocked")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add(account)
    await postgres_session.commit()

    account.nature = "liability"
    account.currency = "USD"
    await postgres_session.commit()
    await postgres_session.refresh(account)

    assert (account.nature, account.currency) == ("liability", "USD")


async def test_database_serializes_transaction_commit_with_tracking_start_change(
    postgres_engine: AsyncEngine,
    postgres_session: AsyncSession,
) -> None:
    owner = _user("tracking-start-concurrency")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add(account)
    await postgres_session.commit()

    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with (
        session_factory() as transaction_session,
        session_factory() as account_session,
        session_factory() as observer_session,
    ):
        transaction = FinanceTransaction(
            ledger_id=ledger.id,
            kind="income",
            transaction_date=date(2026, 8, 1),
            note=None,
        )
        transaction_session.add(transaction)
        await transaction_session.flush()
        transaction_session.add_all(
            [
                FinanceAccountMovement(
                    transaction_id=transaction.id,
                    ledger_id=ledger.id,
                    account_id=account.id,
                    amount=Decimal("10.00"),
                    currency="CNY",
                ),
                FinanceCategoryAllocation(
                    transaction_id=transaction.id,
                    ledger_id=ledger.id,
                    category_id=None,
                    amount=Decimal("10.00"),
                    currency="CNY",
                ),
            ]
        )
        await transaction_session.flush()
        transaction_backend_pid = await transaction_session.scalar(select(func.pg_backend_pid()))
        assert transaction_backend_pid is not None

        competing_account = await account_session.get(FinanceAccount, account.id)
        assert competing_account is not None
        competing_account.tracking_start_date = date(2026, 8, 2)
        await account_session.flush()

        transaction_commit = asyncio.create_task(transaction_session.commit())
        transaction_waited_for_account = False
        for _ in range(100):
            wait_event_type = await observer_session.scalar(
                text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :backend_pid"),
                {"backend_pid": transaction_backend_pid},
            )
            if wait_event_type == "Lock":
                transaction_waited_for_account = True
                break
            if transaction_commit.done():
                break
            await asyncio.sleep(0.01)

        assert transaction_waited_for_account
        await account_session.commit()
        with pytest.raises(IntegrityError):
            await transaction_commit
        await transaction_session.rollback()

        persisted_account = await observer_session.get(FinanceAccount, account.id)
        persisted_transaction_count = await observer_session.scalar(
            select(func.count())
            .select_from(FinanceAccountMovement)
            .where(FinanceAccountMovement.account_id == account.id)
        )

    assert persisted_account is not None
    assert persisted_account.tracking_start_date == date(2026, 8, 2)
    assert persisted_transaction_count == 0


async def test_database_allows_transaction_on_tracking_start_boundary(
    postgres_session: AsyncSession,
) -> None:
    owner = _user("tracking-start-boundary")
    postgres_session.add(owner)
    await postgres_session.flush()
    ledger = _ledger(owner.id, name="Personal", name_key="personal")
    postgres_session.add(ledger)
    await postgres_session.flush()
    account = _account(
        ledger.id,
        nature="asset",
        currency="CNY",
        opening_balance=Decimal("0.00"),
        status="active",
    )
    postgres_session.add(account)
    await postgres_session.commit()

    transaction_id = await create_finance_transaction(
        postgres_session,
        owner_id=owner.id,
        ledger_id=ledger.id,
        kind="income",
        account_id=account.id,
        transaction_date=account.tracking_start_date,
        economic_amount=Money.parse(amount="10.00", currency="CNY"),
        allocation_amount=Money.parse(amount="10.00", currency="CNY"),
        category_id=None,
        note=None,
        project=lambda detail: detail.transaction.id,
    )

    assert await postgres_session.get(FinanceTransaction, transaction_id) is not None


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
