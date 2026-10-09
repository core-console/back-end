"""Opt-in production-preview qualification against protected PostgreSQL.

FINANCE_SUBMISSION_FRONTEND names the delivered consumer checkout. Its production
bundle must already be built. Normal backend tests stay independent of Node/browser
installation; an explicitly requested qualification fails rather than falling back.
"""

import asyncio
import json
import platform
import socket
import subprocess
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import uvicorn
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from core_console.app import create_app
from core_console.config import AuthMode, Environment, Settings
from integration.test_finance_api import _user

pytestmark = pytest.mark.anyio


class BrowserQualificationSettings(BaseSettings):
    """Opt in explicitly without consulting the development dotenv file."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    frontend: Path | None = Field(default=None, validation_alias="FINANCE_SUBMISSION_FRONTEND")


@pytest.fixture
def delivered_consumer() -> tuple[Path, Path, dict[str, str]]:
    consumer_setting = BrowserQualificationSettings().frontend
    if consumer_setting is None:
        pytest.skip("Set FINANCE_SUBMISSION_FRONTEND for real production-preview qualification.")
    consumer = consumer_setting.resolve(strict=True)
    root = Path(__file__).resolve().parents[2]
    assert (consumer / "dist/index.html").is_file(), "Build the delivered frontend first."
    assert not subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=consumer, text=True
    ).strip(), "Qualification requires the clean delivered consumer."
    assert json.loads((consumer / "openapi/openapi.json").read_text(encoding="utf-8")) == (
        json.loads((root / "openapi/openapi.json").read_text(encoding="utf-8"))
    ), "Producer and consumer contracts differ."
    revisions = {
        name: subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True).strip()
        for name, path in (("producer", root), ("consumer", consumer))
    }
    return consumer, root, revisions


async def test_production_browser_recovers_committed_five_operation_outcomes(
    delivered_consumer: tuple[Path, Path, dict[str, str]],
    postgres_database_url: str,
    postgres_engine: AsyncEngine,
    postgres_session: AsyncSession,
) -> None:
    consumer, root, revisions = delivered_consumer
    sessions = async_sessionmaker(postgres_engine, expire_on_commit=False)
    actor = _user("t11-browser")
    async with sessions() as session:
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

    @app.get("/t11-test/evidence/{submission_id}")
    async def committed_evidence(submission_id: UUID) -> dict[str, Any]:
        # This endpoint belongs only to this loopback test app. Each observation
        # uses an independent connection, never the request's uncommitted session.
        async with sessions() as session:
            submission = (
                (
                    await session.execute(
                        text("""SELECT canonical_command, terminal_outcome,
                        admitted_at::text, resolved_at::text, retention_ledger_id::text
                        FROM finance_submissions
                        WHERE local_user_id = :owner AND submission_id = :key"""),
                        {"owner": actor.id, "key": submission_id},
                    )
                )
                .mappings()
                .one()
            )
            ledger = submission["retention_ledger_id"]
            result: dict[str, Any] = {"submission": dict(submission)}
            statements = {
                "ledgers": """SELECT id::text, name FROM finance_ledgers
                    WHERE owner_id = :owner ORDER BY id""",
                "accounts": """SELECT id::text, name, nature, status, opening_balance::text
                    FROM finance_accounts WHERE ledger_id = :ledger ORDER BY id""",
                "categories": """SELECT id::text, name, status FROM finance_categories
                    WHERE ledger_id = :ledger ORDER BY id""",
                "transactions": """SELECT id::text, kind, note FROM finance_transactions
                    WHERE ledger_id = :ledger ORDER BY id""",
                "movements": """SELECT m.account_id::text, m.amount::text
                    FROM finance_account_movements m JOIN finance_transactions t
                    ON t.id = m.transaction_id WHERE t.ledger_id = :ledger
                    ORDER BY m.account_id""",
                "allocations": """SELECT a.amount::text FROM finance_category_allocations a
                    JOIN finance_transactions t ON t.id = a.transaction_id
                    WHERE t.ledger_id = :ledger ORDER BY a.id""",
            }
            for name, sql in statements.items():
                rows = await session.execute(text(sql), {"owner": actor.id, "ledger": ledger})
                result[name] = [dict(row) for row in rows.mappings()]
            result["database"] = (
                (await session.execute(text("SELECT current_database(), version()")))
                .one()
                ._asdict()
            )
            return result

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="warning", access_log=False, lifespan="on")
        )
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            # Psycopg uses SelectorEventLoop on Windows, which has no asyncio
            # subprocess support. Keep process I/O off the server's event loop.
            (root / ".agent").mkdir(exist_ok=True)
            output_path = root / ".agent/t11-node.log"
            with output_path.open("wb") as output:
                process = await asyncio.to_thread(
                    subprocess.run,
                    [
                        "node",
                        str(root / "tests/browser/finance-submissions.mjs"),
                        str(consumer),
                        f"http://127.0.0.1:{port}",
                    ],
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    cwd=root,
                    timeout=240,
                    check=False,
                )
            log = output_path.read_text(encoding="utf-8", errors="replace")
            receipt = root / ".agent/t11-browser.json"
            receipt.parent.mkdir(exist_ok=True)
            receipt.write_text(
                json.dumps(
                    {
                        "revisions": revisions,
                        "python": platform.python_version(),
                        "exitCode": process.returncode,
                        "output": log,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            assert process.returncode == 0, log
            assert '"scenarios":11' in log, log
            for name, path in (("producer", root), ("consumer", consumer)):
                assert (
                    revisions[name]
                    == (
                        await asyncio.to_thread(
                            subprocess.check_output,
                            ["git", "rev-parse", "HEAD"],
                            cwd=path,
                            text=True,
                        )
                    ).strip()
                )
        finally:
            server.should_exit = True
            await task
