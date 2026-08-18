"""Real PostgreSQL coverage for Finance Ledger persistence."""

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
    ReflectedPrimaryKeyConstraint,
    ReflectedUniqueConstraint,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core_console.modules.finance.models import FinanceLedger
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


def _user(subject: str) -> User:
    return User(
        identity_issuer="https://identity.example.test/finance",
        identity_subject=subject,
        status="active",
    )


def _ledger(owner_id: UUID, *, name: str, name_key: str) -> FinanceLedger:
    return FinanceLedger(owner_id=owner_id, name=name, name_key=name_key)
