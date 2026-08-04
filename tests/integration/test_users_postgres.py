"""Real PostgreSQL coverage for the users persistence schema."""

from http import HTTPStatus
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from httpx import ASGITransport, AsyncClient, Response
from pydantic import SecretStr
from sqlalchemy import Connection, inspect, text
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.engine.interfaces import (
    ReflectedCheckConstraint,
    ReflectedColumn,
    ReflectedPrimaryKeyConstraint,
    ReflectedUniqueConstraint,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core_console.app import create_app
from core_console.config import AuthMode, Environment, Settings
from core_console.modules.users.models import User
from core_console.modules.users.queries import get_user_by_identity

pytestmark = pytest.mark.anyio

ALEMBIC_CONFIG_PATH = Path(__file__).resolve().parents[2] / "alembic.ini"


async def get_me(
    *,
    database_url: str,
    identity_issuer: str,
    identity_subject: str,
) -> Response:
    """Call the current-user API against the protected PostgreSQL target."""

    app = create_app(
        Settings(
            environment=Environment.TEST,
            auth_mode=AuthMode.DEVELOPMENT,
            dev_identity_issuer=identity_issuer,
            dev_identity_subject=identity_subject,
            database_url=SecretStr(database_url),
            database_connect_timeout_seconds=0.1,
        )
    )
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/api/me")


class UserSchemaInspection(TypedDict):
    """Typed subset of SQLAlchemy's PostgreSQL inspector output."""

    tables: set[str]
    columns: list[ReflectedColumn]
    primary_key: ReflectedPrimaryKeyConstraint
    unique_constraints: list[ReflectedUniqueConstraint]
    check_constraints: list[ReflectedCheckConstraint]


def inspect_users(sync_connection: Connection) -> UserSchemaInspection:
    """Return the users schema inspection needed by the migration test."""

    schema_inspector = inspect(sync_connection)
    return {
        "tables": set(schema_inspector.get_table_names()),
        "columns": schema_inspector.get_columns("users"),
        "primary_key": schema_inspector.get_pk_constraint("users"),
        "unique_constraints": schema_inspector.get_unique_constraints("users"),
        "check_constraints": schema_inspector.get_check_constraints("users"),
    }


async def test_empty_database_upgrades_to_head_and_creates_users_schema(
    postgres_engine: AsyncEngine,
) -> None:
    """A protected empty database can reach Alembic head with the users schema."""

    alembic_config = Config(str(ALEMBIC_CONFIG_PATH))
    alembic_head = ScriptDirectory.from_config(alembic_config).get_current_head()

    async with postgres_engine.connect() as connection:
        version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        user_count = await connection.scalar(text("SELECT count(*) FROM users"))
        schema = await connection.run_sync(lambda sync_connection: inspect_users(sync_connection))

    assert version == alembic_head
    assert user_count == 0
    assert {"alembic_version", "users"} <= schema["tables"]
    columns = {column["name"]: column for column in schema["columns"]}
    assert set(columns) == {
        "id",
        "identity_issuer",
        "identity_subject",
        "username",
        "display_name",
        "email",
        "status",
        "created_at",
        "updated_at",
    }
    assert all(
        columns[name]["nullable"] is False
        for name in (
            "id",
            "identity_issuer",
            "identity_subject",
            "status",
            "created_at",
            "updated_at",
        )
    )
    assert all(columns[name]["nullable"] is True for name in ("username", "display_name", "email"))
    assert isinstance(columns["id"]["type"], PostgreSQLUUID)
    assert columns["status"]["default"] is None
    assert getattr(columns["created_at"]["type"], "timezone", False) is True
    assert getattr(columns["updated_at"]["type"], "timezone", False) is True
    assert schema["primary_key"]["constrained_columns"] == ["id"]
    assert {("identity_issuer", "identity_subject")} <= {
        tuple(constraint["column_names"]) for constraint in schema["unique_constraints"]
    }
    assert {
        "ck_users_identity_issuer_not_blank",
        "ck_users_identity_subject_not_blank",
        "ck_users_status",
    } <= {constraint["name"] for constraint in schema["check_constraints"]}


async def test_identity_key_is_unique(postgres_session: AsyncSession) -> None:
    """PostgreSQL rejects a duplicate issuer/subject pair."""

    identity_issuer = f"issuer-{uuid4().hex}"
    identity_subject = f"subject-{uuid4().hex}"
    postgres_session.add(
        User(
            identity_issuer=identity_issuer,
            identity_subject=identity_subject,
            status="active",
        )
    )
    await postgres_session.commit()

    postgres_session.add(
        User(
            identity_issuer=identity_issuer,
            identity_subject=identity_subject,
            status="disabled",
        )
    )
    with pytest.raises(IntegrityError):
        await postgres_session.commit()


@pytest.mark.parametrize("blank_field", ("issuer", "subject"))
async def test_blank_identity_parts_are_rejected(
    postgres_session: AsyncSession,
    blank_field: str,
) -> None:
    """PostgreSQL rejects identity components that trim to an empty string."""

    values = {
        "identity_issuer": f"issuer-{uuid4().hex}",
        "identity_subject": f"subject-{uuid4().hex}",
        "status": "active",
    }
    values[f"identity_{blank_field}"] = " \t\n "
    postgres_session.add(User(**values))

    with pytest.raises(IntegrityError):
        await postgres_session.commit()


async def test_status_is_required_and_constrained(postgres_session: AsyncSession) -> None:
    """PostgreSQL rejects both an omitted status and an unknown status value."""

    postgres_session.add(
        User(
            identity_issuer=f"issuer-{uuid4().hex}",
            identity_subject=f"subject-{uuid4().hex}",
            status="pending",
        )
    )
    with pytest.raises(IntegrityError):
        await postgres_session.commit()
    await postgres_session.rollback()

    postgres_session.add(
        User(
            identity_issuer=f"issuer-{uuid4().hex}",
            identity_subject=f"subject-{uuid4().hex}",
        )
    )
    with pytest.raises(IntegrityError):
        await postgres_session.commit()


async def test_identity_query_distinguishes_issuer_subject_and_status(
    postgres_session: AsyncSession,
) -> None:
    """The dedicated query returns active, disabled, and missing users by both keys."""

    shared_issuer = f"issuer-{uuid4().hex}"
    shared_subject = f"subject-{uuid4().hex}"
    active_user = User(
        identity_issuer=shared_issuer,
        identity_subject=shared_subject,
        status="active",
    )
    disabled_user = User(
        identity_issuer=shared_issuer,
        identity_subject=f"subject-{uuid4().hex}",
        status="disabled",
    )
    other_issuer_user = User(
        identity_issuer=f"issuer-{uuid4().hex}",
        identity_subject=shared_subject,
        status="active",
    )
    postgres_session.add_all([active_user, disabled_user, other_issuer_user])
    await postgres_session.commit()

    found_active = await get_user_by_identity(
        postgres_session,
        identity_issuer=shared_issuer,
        identity_subject=shared_subject,
    )
    found_disabled = await get_user_by_identity(
        postgres_session,
        identity_issuer=shared_issuer,
        identity_subject=disabled_user.identity_subject,
    )
    found_other_issuer = await get_user_by_identity(
        postgres_session,
        identity_issuer=other_issuer_user.identity_issuer,
        identity_subject=shared_subject,
    )
    missing = await get_user_by_identity(
        postgres_session,
        identity_issuer=shared_issuer,
        identity_subject=f"missing-{uuid4().hex}",
    )

    assert found_active is not None
    assert found_active.id == active_user.id
    assert found_active.status == "active"
    assert found_disabled is not None
    assert found_disabled.id == disabled_user.id
    assert found_disabled.status == "disabled"
    assert found_other_issuer is not None
    assert found_other_issuer.id == other_issuer_user.id
    assert missing is None
    assert active_user.created_at.tzinfo is not None
    assert active_user.updated_at.tzinfo is not None


async def test_active_persisted_user_can_get_own_public_profile(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    """The fixed development identity resolves through PostgreSQL to GET /api/me."""

    identity_issuer = f"issuer-{uuid4().hex}"
    identity_subject = f"subject-{uuid4().hex}"
    user = User(
        identity_issuer=identity_issuer,
        identity_subject=identity_subject,
        username="local-developer",
        display_name=None,
        email="developer@example.test",
        status="active",
    )
    postgres_session.add(user)
    await postgres_session.commit()

    response = await get_me(
        database_url=postgres_database_url,
        identity_issuer=identity_issuer,
        identity_subject=identity_subject,
    )

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "id": str(user.id),
        "username": "local-developer",
        "displayName": None,
        "email": "developer@example.test",
    }


async def test_missing_and_disabled_persisted_users_have_the_same_api_result(
    postgres_database_url: str,
    postgres_session: AsyncSession,
) -> None:
    """Provisioning state is not disclosed by the current-user API."""

    identity_issuer = f"issuer-{uuid4().hex}"
    disabled_user = User(
        identity_issuer=identity_issuer,
        identity_subject=f"subject-{uuid4().hex}",
        status="disabled",
    )
    postgres_session.add(disabled_user)
    await postgres_session.commit()

    disabled_response = await get_me(
        database_url=postgres_database_url,
        identity_issuer=identity_issuer,
        identity_subject=disabled_user.identity_subject,
    )
    missing_response = await get_me(
        database_url=postgres_database_url,
        identity_issuer=identity_issuer,
        identity_subject=f"missing-{uuid4().hex}",
    )

    assert disabled_response.status_code == HTTPStatus.FORBIDDEN
    assert disabled_response.json() == missing_response.json()
    assert disabled_response.json()["code"] == "access_denied"
