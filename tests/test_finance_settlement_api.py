"""Cash repayment preview preserves the Finance database failure taxonomy."""

from collections.abc import AsyncIterator
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from psycopg import OperationalError as DriverError
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.database.dependencies import get_session
from core_console.modules.users.dependencies import get_current_user
from core_console.modules.users.identity import CurrentUser

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("unavailable", [True, False])
async def test_preview_translates_ledger_query_failures_at_the_http_boundary(
    app: FastAPI, client: AsyncClient, unavailable: bool
) -> None:
    session = cast(AsyncSession, AsyncMock(spec=AsyncSession))
    cast(AsyncMock, session.scalar).side_effect = (
        OperationalError(None, None, DriverError("connection refused"), connection_invalidated=True)
        if unavailable
        else RuntimeError("query mapping defect")
    )

    async def failing_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = failing_session
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=uuid4(), username=None, display_name=None, email=None, status="active"
    )
    response = await client.post(f"/api/finance/ledgers/{uuid4()}/settlements/preview", json={})
    assert response.status_code == (503 if unavailable else 500)
    assert response.json()["code"] == ("database_unavailable" if unavailable else "internal_error")
    assert response.headers["content-type"] == "application/problem+json"
    assert "submissionReceipt" not in response.json()
    assert "commandValidationRejection" not in response.json()
