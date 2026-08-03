"""Request-scoped AsyncSession dependency tests."""

from collections.abc import AsyncGenerator
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from core_console.config import AuthMode, Environment, Settings
from core_console.database.dependencies import get_session
from core_console.database.resources import DatabaseResources
from core_console.resources import ApplicationResources


def request_with_sessions(
    session_factory: async_sessionmaker[AsyncSession],
) -> Request:
    """Create a minimal request carrying typed application resources."""

    app = FastAPI()
    app.state.resources = ApplicationResources(
        settings=Settings(
            environment=Environment.TEST,
            auth_mode=AuthMode.DEVELOPMENT,
            dev_identity_issuer="https://identity.example.test",
            dev_identity_subject="test-developer",
            database_url=None,
            database_connect_timeout_seconds=0.1,
        ),
        database=DatabaseResources(
            engine=cast(AsyncEngine, Mock()),
            sessions=session_factory,
        ),
    )
    return Request(
        {
            "type": "http",
            "app": app,
            "method": "GET",
            "path": "/",
            "headers": [],
        }
    )


@pytest.mark.anyio
async def test_each_dependency_call_uses_and_closes_an_independent_session() -> None:
    session_one = cast(AsyncSession, AsyncMock(spec=AsyncSession))
    session_two = cast(AsyncSession, AsyncMock(spec=AsyncSession))
    context_one = AsyncMock()
    context_one.__aenter__.return_value = session_one
    context_two = AsyncMock()
    context_two.__aenter__.return_value = session_two
    session_factory_mock = Mock(side_effect=[context_one, context_two])
    request = request_with_sessions(cast(async_sessionmaker[AsyncSession], session_factory_mock))

    dependency_one = cast(AsyncGenerator[AsyncSession], get_session(request))
    dependency_two = cast(AsyncGenerator[AsyncSession], get_session(request))
    yielded_one = await anext(dependency_one)
    yielded_two = await anext(dependency_two)

    assert yielded_one is session_one
    assert yielded_two is session_two
    assert yielded_one is not yielded_two

    await dependency_one.aclose()
    await dependency_two.aclose()

    assert session_factory_mock.call_count == 2
    context_one.__aexit__.assert_awaited_once()
    context_two.__aexit__.assert_awaited_once()


@pytest.mark.anyio
async def test_session_context_closes_without_committing_on_request_error() -> None:
    session = cast(AsyncSession, AsyncMock(spec=AsyncSession))
    context = AsyncMock()
    context.__aenter__.return_value = session
    session_factory_mock = Mock(return_value=context)
    request = request_with_sessions(cast(async_sessionmaker[AsyncSession], session_factory_mock))
    dependency = cast(AsyncGenerator[AsyncSession], get_session(request))
    await anext(dependency)

    with pytest.raises(RuntimeError, match="request failed"):
        await dependency.athrow(RuntimeError("request failed"))

    context.__aexit__.assert_awaited_once()
    cast(AsyncMock, session.commit).assert_not_awaited()
