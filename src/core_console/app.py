"""Core Console application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from core_console.config import Settings, load_settings
from core_console.database.resources import DatabaseResources
from core_console.health.api import router as health_router
from core_console.modules.hello.api import router as hello_router
from core_console.openapi import CoreConsoleApp
from core_console.problems import install_problem_handlers
from core_console.resources import ApplicationResources


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create an isolated application without connecting to external services."""

    resolved_settings = settings if settings is not None else load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database_url = resolved_settings.database_url_value()
        database = DatabaseResources.create(database_url) if database_url is not None else None
        app.state.resources = ApplicationResources(
            settings=resolved_settings,
            database=database,
        )
        try:
            yield
        finally:
            if database is not None:
                await database.dispose()

    app = CoreConsoleApp(
        title="Core Console API",
        version="0.1.0",
        lifespan=lifespan,
        openapi_url="/api/openapi.json",
        docs_url="/api/docs",
        redoc_url=None,
    )
    install_problem_handlers(app)
    app.include_router(hello_router)
    app.include_router(health_router)
    return app
