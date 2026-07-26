"""Local development command tests."""

from unittest.mock import Mock

import pytest

from core_console.cli import start


def test_start_runs_application_factory_with_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock()
    monkeypatch.setattr("core_console.cli.uvicorn.run", run)

    start()

    run.assert_called_once_with(
        "core_console.app:create_app",
        factory=True,
        reload=True,
        access_log=False,
    )
