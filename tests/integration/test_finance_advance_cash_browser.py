"""Opt-in T14 production-browser check against guarded PostgreSQL."""

import asyncio
import json
import os
import socket
import subprocess
from pathlib import Path

import pytest
import uvicorn
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from core_console.app import create_app
from core_console.config import AuthMode, Environment, Settings
from integration.test_finance_api import _user

pytestmark = pytest.mark.anyio


@pytest.fixture
def browser_config() -> tuple[Path, str, str]:
    frontend = os.environ.get("FINANCE_T14_TEST_FRONTEND")
    if not frontend:
        pytest.skip("Set FINANCE_T14_TEST_FRONTEND to run the real browser workflow.")
    node = os.environ["FINANCE_T14_TEST_NODE"]
    pnpm = os.environ["FINANCE_T14_TEST_PNPM"]
    root = Path(__file__).resolve().parents[2]
    consumer = Path(frontend).resolve(strict=True)
    assert json.loads((root / "openapi/openapi.json").read_text(encoding="utf-8")) == json.loads(
        (consumer / "openapi/openapi.json").read_text(encoding="utf-8")
    )
    return consumer, node, pnpm


async def test_real_browser_cash_correction(
    browser_config: tuple[Path, str, str],
    postgres_database_url: str,
    postgres_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    consumer, node, pnpm = browser_config
    actor = _user("t14-browser")
    async with async_sessionmaker(postgres_engine, expire_on_commit=False)() as session:
        session.add(actor)
        await session.commit()
    app = create_app(
        Settings(
            environment=Environment.TEST,
            auth_mode=AuthMode.DEVELOPMENT,
            dev_identity_issuer=actor.identity_issuer,
            dev_identity_subject=actor.identity_subject,
            database_url=SecretStr(postgres_database_url),
        )
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.05)
            assert server.started, "The local test backend did not start."
            environment = {
                **os.environ,
                "FINANCE_T14_TEST_BACKEND": f"http://127.0.0.1:{port}",
                "TEMP": str(tmp_path),
                "TMP": str(tmp_path),
            }
            result = await asyncio.to_thread(
                subprocess.run,
                [
                    node,
                    pnpm,
                    "exec",
                    "playwright",
                    "test",
                    "e2e/advance-cash-corrections-integrated.spec.ts",
                    "--project=chromium",
                    "--workers=1",
                    "--reporter=line",
                    f"--output={tmp_path / 'browser-results'}",
                ],
                cwd=consumer,
                env=environment,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=180,
                check=False,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            assert "1 passed" in result.stdout, result.stdout + result.stderr
        finally:
            server.should_exit = True
            await task
