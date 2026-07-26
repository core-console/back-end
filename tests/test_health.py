"""Liveness and truthful readiness tests."""

from http import HTTPStatus
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.exc import SQLAlchemyError

from core_console.database.resources import DatabaseResources
from core_console.resources import ApplicationResources, get_application_resources


@pytest.mark.anyio
async def test_liveness_does_not_require_database(client: AsyncClient) -> None:
    response = await client.get("/health/live")

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"status": "alive"}


@pytest.mark.anyio
async def test_readiness_fails_when_database_is_not_configured(client: AsyncClient) -> None:
    response = await client.get("/health/ready")

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "database_not_configured"


@pytest.mark.anyio
async def test_readiness_succeeds_when_probe_succeeds(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    resources = get_application_resources(app)
    database = Mock(spec=DatabaseResources)
    database.ping = AsyncMock()
    app.state.resources = ApplicationResources(
        settings=resources.settings,
        database=cast(DatabaseResources, database),
    )

    response = await client.get("/health/live")
    assert response.status_code == HTTPStatus.OK
    database.ping.assert_not_awaited()

    response = await client.get("/health/ready")

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"status": "ready"}
    database.ping.assert_awaited_once_with(timeout_seconds=0.1)


@pytest.mark.anyio
async def test_readiness_fails_when_database_is_unreachable(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    resources = get_application_resources(app)
    database = Mock(spec=DatabaseResources)
    database.ping = AsyncMock(side_effect=SQLAlchemyError("connection refused"))
    app.state.resources = ApplicationResources(
        settings=resources.settings,
        database=cast(DatabaseResources, database),
    )

    response = await client.get("/health/ready")

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["status"] == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["code"] == "database_unavailable"
