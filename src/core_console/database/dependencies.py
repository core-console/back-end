"""FastAPI dependencies for request-scoped database sessions."""

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.problems import ApplicationProblem
from core_console.resources import get_application_resources


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield one session per request without creating implicit infrastructure."""

    database = get_application_resources(request.app).database
    if database is None:
        raise ApplicationProblem(
            status=503,
            title="Service Unavailable",
            detail="PostgreSQL is not configured.",
            code="database_not_configured",
        )

    async with database.sessions() as session:
        yield session
