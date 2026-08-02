"""Tests for the async event loop used by integration tests."""

import asyncio
import sys

import pytest

pytestmark = pytest.mark.anyio


async def test_windows_integration_tests_use_selector_event_loop() -> None:
    """Windows integration tests must use Psycopg's supported event loop."""

    if sys.platform == "win32":
        assert isinstance(asyncio.get_running_loop(), asyncio.SelectorEventLoop)
