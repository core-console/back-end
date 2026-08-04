"""Regression tests for the formal Alembic migration runner."""

import asyncio
import sys
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config

ALEMBIC_CONFIG_PATH = Path(__file__).resolve().parents[1] / "alembic.ini"


def _alembic_config() -> Config:
    """Return the repository Alembic configuration used by the CLI."""

    return Config(str(ALEMBIC_CONFIG_PATH))


def _run_online_without_database(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, object]]:
    """Run the online Alembic path while recording the asyncio runner call."""

    calls: list[dict[str, object]] = []

    def fake_run(coroutine: Coroutine[Any, Any, None], **kwargs: object) -> None:
        coroutine.close()
        calls.append(kwargs)

    monkeypatch.setattr(asyncio, "run", fake_run)
    command.upgrade(_alembic_config(), "head")
    return calls


def test_windows_alembic_runner_uses_selector_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Windows online runner must pass Psycopg a SelectorEventLoop factory."""

    monkeypatch.setattr(sys, "platform", "win32")

    calls = _run_online_without_database(monkeypatch)

    assert calls == [{"loop_factory": asyncio.SelectorEventLoop}]


def test_non_windows_alembic_runner_keeps_default_asyncio_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-Windows online migrations must keep asyncio's default runner behavior."""

    monkeypatch.setattr(sys, "platform", "linux")

    calls = _run_online_without_database(monkeypatch)

    assert calls == [{}]


def test_offline_migrations_do_not_start_asyncio_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Offline migrations must remain independent of the async runner."""

    monkeypatch.setenv("AUTH_MODE", "development")
    monkeypatch.setenv("DEV_IDENTITY_ISSUER", "https://identity.example.test")
    monkeypatch.setenv("DEV_IDENTITY_SUBJECT", "test-developer")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://test:test@127.0.0.1:5432/test",
    )

    def fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("offline migrations must not call asyncio.run")

    monkeypatch.setattr(asyncio, "run", fail_if_called)
    command.upgrade(_alembic_config(), "head", sql=True)
