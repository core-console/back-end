"""Application factory and startup tests."""

from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from core_console.app import create_app
from core_console.config import AuthMode, Environment, Settings
from core_console.database.resources import DatabaseResources
from core_console.resources import get_application_resources


@pytest.mark.anyio
async def test_application_starts_without_database(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_database = Mock()
    monkeypatch.setattr(DatabaseResources, "create", create_database)

    async with app.router.lifespan_context(app):
        resources = get_application_resources(app)
        assert resources.database is None

    create_database.assert_not_called()


@pytest.mark.anyio
async def test_lifespan_creates_one_engine_and_disposes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Mock(spec=DatabaseResources)
    database.dispose = AsyncMock()
    create_database = Mock(return_value=database)
    monkeypatch.setattr(DatabaseResources, "create", create_database)
    settings = Settings(
        environment=Environment.TEST,
        auth_mode=AuthMode.DEVELOPMENT,
        dev_identity_issuer="https://identity.example.test",
        dev_identity_subject="test-developer",
        database_url=SecretStr("postgresql+psycopg://core_console:not-real@localhost/core_console"),
        database_connect_timeout_seconds=0.1,
    )
    app = create_app(settings)

    create_database.assert_not_called()

    with pytest.raises(RuntimeError, match="lifespan failure"):
        async with app.router.lifespan_context(app):
            resources = get_application_resources(app)
            assert resources.database is database
            create_database.assert_called_once_with(settings.database_url_value())
            raise RuntimeError("lifespan failure")

    database.dispose.assert_awaited_once_with()
