"""Application factory and startup tests."""

from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from core_console.app import create_app
from core_console.config import Environment, Settings
from core_console.database.resources import DatabaseResources
from core_console.http_logging import HttpRequestLoggingMiddleware
from core_console.problems import UnexpectedExceptionMiddleware
from core_console.resources import get_application_resources


def test_application_factory_creates_fastapi_app() -> None:
    settings = Settings(
        environment=Environment.TEST,
        database_url=None,
        database_connect_timeout_seconds=0.1,
    )

    app = create_app(settings)

    assert isinstance(app, FastAPI)


def test_http_middleware_order(app: FastAPI) -> None:
    assert [getattr(middleware.cls, "__name__", None) for middleware in app.user_middleware] == [
        HttpRequestLoggingMiddleware.__name__,
        UnexpectedExceptionMiddleware.__name__,
    ]


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
