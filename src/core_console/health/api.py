"""Liveness and readiness routes outside the frontend business API."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError

from core_console.problems import ApplicationProblem
from core_console.resources import get_application_resources

router = APIRouter(prefix="/health", include_in_schema=False)


class HealthResponse(BaseModel):
    """Small operational health response."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["alive", "ready"]


@router.get("/live", response_model=HealthResponse)
async def live() -> HealthResponse:
    """Report process liveness without touching external dependencies."""

    return HealthResponse(status="alive")


async def check_readiness(request: Request) -> None:
    """Verify that every currently required external dependency is ready."""

    resources = get_application_resources(request.app)
    if resources.database is None:
        raise ApplicationProblem(
            status=503,
            title="Service Unavailable",
            detail="PostgreSQL is not configured.",
            code="database_not_configured",
        )

    try:
        await resources.database.ping(
            timeout_seconds=resources.settings.database_connect_timeout_seconds
        )
    except SQLAlchemyError, TimeoutError:
        raise ApplicationProblem(
            status=503,
            title="Service Unavailable",
            detail="PostgreSQL is not ready.",
            code="database_unavailable",
        ) from None


@router.get("/ready", response_model=HealthResponse)
async def ready(_: Annotated[None, Depends(check_readiness)]) -> HealthResponse:
    """Report readiness only after the database probe succeeds."""

    return HealthResponse(status="ready")
