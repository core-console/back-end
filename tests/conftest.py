"""Shared in-process ASGI test fixtures."""

import asyncio
import logging
import platform
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core_console.app import create_app
from core_console.config import Environment, Settings


@pytest.fixture
def anyio_backend() -> str | tuple[str, dict[str, object]]:
    """Keep async tests on a Psycopg-compatible standard-library event loop."""

    if platform.system() == "Windows":
        return ("asyncio", {"loop_factory": asyncio.SelectorEventLoop})
    return "asyncio"


@pytest.fixture
def settings() -> Settings:
    """Return explicit test settings that never consult real infrastructure."""

    return Settings(
        environment=Environment.TEST,
        database_url=None,
        database_connect_timeout_seconds=0.1,
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    """Create one isolated application per test."""

    return create_app(settings)


@pytest.fixture
def application_log_records(
    caplog: pytest.LogCaptureFixture,
) -> Iterator[pytest.LogCaptureFixture]:
    """Capture application logs despite their production propagation boundary."""

    application_logger = logging.getLogger("core_console")
    application_logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        application_logger.removeHandler(caplog.handler)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Start lifespan and send requests through HTTPX without real networking."""

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http
