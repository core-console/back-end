"""Exercise the real PostgreSQL lease and destructive fixture recovery."""

import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import psycopg
import pytest
from scripts.test_postgres import prepare_postgres
from sqlalchemy import Connection, Engine, create_engine, event, make_url, text
from sqlalchemy.exc import OperationalError, StatementError
from sqlalchemy.ext.asyncio import AsyncEngine

from .conftest import TestDatabaseSettings as _TestDatabaseSettings
from .conftest import _validated_test_database_url

ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = ROOT / "alembic.ini"
OWNER_CHILD = """
import sys
import time
from pathlib import Path

from scripts.test_postgres import prepare_postgres
from tests.integration.conftest import _validated_test_database_url

with prepare_postgres(
    _validated_test_database_url(), Path('alembic.ini'),
    record=lambda stage: None, observe_sql=lambda: None,
):
    if len(sys.argv) > 1:
        Path(sys.argv[1]).touch()
        time.sleep(60)
"""


@pytest.fixture
def serial_database() -> Iterator[str]:
    if _TestDatabaseSettings().recovery_opt_in != "1":
        pytest.skip("Run the PostgreSQL recovery tests serially with the documented opt-in.")
    yield _validated_test_database_url()


@pytest.mark.parametrize("opt_in", [None, "0", "true"])
def test_recovery_fixture_requires_explicit_opt_in(
    opt_in: str | None, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    if opt_in is None:
        monkeypatch.delenv("CORE_CONSOLE_TEST_POSTGRES_RECOVERY", raising=False)
    else:
        monkeypatch.setenv("CORE_CONSOLE_TEST_POSTGRES_RECOVERY", opt_in)
    with pytest.raises(pytest.skip.Exception, match="documented opt-in"):
        request.getfixturevalue("serial_database")


def schema_oid(url: str) -> str:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return str(
                connection.exec_driver_sql("SELECT 'public'::regnamespace::oid").scalar_one()
            )
    finally:
        engine.dispose()


def contender() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (sys.executable, "-c", OWNER_CHILD),
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )


def test_concurrent_reset_refused_through_fixture_teardown(postgres_database_url: str) -> None:
    before = schema_oid(postgres_database_url)
    result = contender()
    assert result.returncode != 0
    assert "exclusive ownership of the disposable PostgreSQL database is busy" in result.stderr
    assert schema_oid(postgres_database_url) == before


def test_killed_owner_releases_database_for_a_fresh_reset(
    serial_database: str, tmp_path: Path
) -> None:
    ready = tmp_path / "lease-acquired"
    with subprocess.Popen(
        (sys.executable, "-c", OWNER_CHILD, str(ready)),
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        try:
            deadline = time.monotonic() + 20
            while not ready.is_file() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            assert ready.is_file(), "owner never completed initialization"
            before = schema_oid(serial_database)
            blocked = contender()
            assert blocked.returncode != 0
            assert "exclusive ownership" in blocked.stderr
            assert schema_oid(serial_database) == before
        finally:
            process.kill()
            process.communicate(timeout=10)

    deadline = time.monotonic() + 10
    while True:
        recovered = contender()
        if recovered.returncode == 0:
            break
        assert "exclusive ownership" in recovered.stderr and time.monotonic() < deadline, (
            recovered.stderr
        )
    assert schema_oid(serial_database) != before


def test_live_client_still_fences_reset_after_owner_backend_loss(serial_database: str) -> None:
    stages: list[dict[str, object]] = []
    engine = create_engine(serial_database)
    try:
        with (
            pytest.raises(OperationalError),
            prepare_postgres(
                serial_database,
                ALEMBIC_INI,
                record=stages.append,
                observe_sql=lambda: None,
            ),
            engine.connect() as client,
        ):
            pid = stages[0]["backendPid"]
            assert isinstance(pid, int)
            terminated = client.execute(
                text("SELECT pg_terminate_backend(:pid, 5000)"), {"pid": pid}
            ).scalar_one()
            assert terminated is True
            blocked = contender()
            assert blocked.returncode != 0
            assert "exclusive ownership" in blocked.stderr
    finally:
        engine.dispose()
    assert [stage["stage"] for stage in stages] == ["connected", "acquired", "initialized"]
    recovered = contender()
    assert recovered.returncode == 0, recovered.stderr


_CLIENT_LOCK_SQL = """
    SELECT EXISTS (
        SELECT FROM pg_locks
        WHERE locktype = 'advisory' AND granted AND mode = 'ShareLock'
          AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
          AND classid = 1128485971 AND objid = 2 AND objsubid = 2
          AND pid = %s
    )
"""
_LOCKED_BACKEND_SQL = (
    "SELECT pg_backend_pid() AS pid, ("
    + _CLIENT_LOCK_SQL.replace("%s", "pg_backend_pid()")
    + ") AS locked"
)


def test_revalidated_client_fences_reset_until_its_session_ends(
    request: pytest.FixtureRequest,
) -> None:
    serial_database: str = request.getfixturevalue("serial_database")
    stages: list[dict[str, object]] = []
    schema_identity = "SELECT 'public'::regnamespace::oid::text"
    engine = create_engine(serial_database, pool_size=1, max_overflow=0)
    observer_url = make_url(serial_database).set(drivername="postgresql")
    try:
        # This independent observer runs only catalog reads and terminates the test's own owner.
        with (
            psycopg.connect(
                observer_url.render_as_string(hide_password=False), autocommit=True
            ) as observer,
            ExitStack() as ownership,
        ):
            ownership.enter_context(
                prepare_postgres(
                    serial_database,
                    ALEMBIC_INI,
                    record=stages.append,
                    observe_sql=lambda: None,
                )
            )
            initialized_oid = stages[-1]["schemaOidAfter"]
            assert isinstance(initialized_oid, str)
            with engine.connect() as client:
                original_pid = client.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
                assert observer.execute(_CLIENT_LOCK_SQL, (original_pid,)).fetchone() == (True,)

            for _ in range(2):
                with engine.connect() as client:
                    assert (
                        client.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
                        == original_pid
                    )
                    assert observer.execute(_CLIENT_LOCK_SQL, (original_pid,)).fetchone() == (True,)

            with engine.connect() as client:
                # PostgreSQL displays one pg_locks row even for reentrant session locks.
                # A single unlock must remove the row after repeated checkout of this PID.
                assert (
                    client.exec_driver_sql(
                        "SELECT pg_advisory_unlock_shared(1128485971, 2)"
                    ).scalar_one()
                    is True
                )
                assert observer.execute(_CLIENT_LOCK_SQL, (original_pid,)).fetchone() == (False,)
                client.rollback()
                client.invalidate()

                # The independent observer checks the new backend after pool checkout,
                # before this wrapper can send its first application statement.
                observed_pids: list[int] = []

                def verify_replacement(connection: Connection, *_args: object) -> None:
                    driver_connection = connection.connection.driver_connection
                    assert isinstance(driver_connection, psycopg.Connection)
                    backend_pid = driver_connection.info.backend_pid
                    assert backend_pid != original_pid
                    assert observer.execute(_CLIENT_LOCK_SQL, (backend_pid,)).fetchone() == (True,)
                    observed_pids.append(backend_pid)

                event.listen(client, "before_cursor_execute", verify_replacement)
                try:
                    replacement = client.exec_driver_sql(_LOCKED_BACKEND_SQL).one()
                finally:
                    event.remove(client, "before_cursor_execute", verify_replacement)
                assert observed_pids == [replacement[0]]
                assert replacement[1] is True
                client.rollback()

                blocked = contender()
                assert blocked.returncode != 0 and "exclusive ownership" in blocked.stderr
                assert observer.execute(schema_identity).fetchone() == (initialized_oid,)
            # Checkin leaves the replacement physical session and its fence alive.
            assert observer.execute(_CLIENT_LOCK_SQL, (replacement[0],)).fetchone() == (True,)
            with engine.connect() as client:
                assert (
                    client.exec_driver_sql("SELECT pg_backend_pid()").scalar_one() == replacement[0]
                )
                client.rollback()
                owner_pid = stages[0]["backendPid"]
                assert isinstance(owner_pid, int)
                assert observer.execute(
                    "SELECT pg_terminate_backend(%s, 5000)", (owner_pid,)
                ).fetchone() == (True,)
                blocked = contender()
                assert blocked.returncode != 0 and "exclusive ownership" in blocked.stderr
                assert observer.execute(schema_identity).fetchone() == (initialized_oid,)

                client.invalidate()
                with pytest.raises(StatementError, match="ownership was lost") as rejected:
                    client.exec_driver_sql("SELECT 1")
                assert isinstance(rejected.value.orig, RuntimeError)
            assert observer.execute(_CLIENT_LOCK_SQL, (replacement[0],)).fetchone() == (False,)
            with pytest.raises(OperationalError) as owner_error:
                ownership.close()
            assert owner_error.value.connection_invalidated
    finally:
        engine.dispose()

    assert [stage["stage"] for stage in stages] == ["connected", "acquired", "initialized"]
    recovered = contender()
    assert recovered.returncode == 0, recovered.stderr
    assert schema_oid(serial_database) != initialized_oid


@pytest.mark.anyio
async def test_async_revalidation_and_pool_disposal_preserve_fencing(
    postgres_engine: AsyncEngine,
) -> None:
    async with postgres_engine.connect() as client:
        first = (await client.exec_driver_sql(_LOCKED_BACKEND_SQL)).mappings().one()
        assert first["locked"] is True
        await client.rollback()
        await client.invalidate()
        replacement = (await client.exec_driver_sql(_LOCKED_BACKEND_SQL)).mappings().one()
        assert replacement["pid"] != first["pid"]
        assert replacement["locked"] is True
    await postgres_engine.dispose()
    async with postgres_engine.connect() as client:
        after_disposal = (await client.exec_driver_sql(_LOCKED_BACKEND_SQL)).mappings().one()
        assert after_disposal["pid"] not in (first["pid"], replacement["pid"])
        assert after_disposal["locked"] is True
        old_lock = await client.exec_driver_sql(_CLIENT_LOCK_SQL, (replacement["pid"],))
        assert old_lock.scalar_one() is False


def test_failed_migration_rolls_back_the_schema_reset(serial_database: str, tmp_path: Path) -> None:
    before = schema_oid(serial_database)
    versions = tmp_path / "versions"
    versions.mkdir()
    (versions / "broken.py").write_text(
        "revision = 'broken'\ndown_revision = None\n", encoding="utf-8"
    )
    (tmp_path / "env.py").write_text("raise RuntimeError('migration failed')\n", encoding="utf-8")
    config = tmp_path / "alembic.ini"
    config.write_text("[alembic]\nscript_location = %(here)s\n", encoding="utf-8")
    stages: list[dict[str, object]] = []

    with (
        pytest.raises(RuntimeError, match="migration failed"),
        prepare_postgres(serial_database, config, record=stages.append, observe_sql=lambda: None),
    ):
        pytest.fail("a failed migration reached the test body")
    assert [stage["stage"] for stage in stages] == ["connected", "acquired"]
    assert schema_oid(serial_database) == before
    recovery = contender()
    assert recovery.returncode == 0, recovery.stderr


def test_rejects_a_connection_to_the_wrong_database(serial_database: str) -> None:
    before = schema_oid(serial_database)
    target = make_url(serial_database).set(database="incorrect_test")
    stages: list[dict[str, object]] = []

    def misroute(_dialect: Any, _record: Any, _args: Any, parameters: dict[str, Any]) -> None:
        if parameters.get("dbname") == "incorrect_test":
            parameters["dbname"] = make_url(serial_database).database

    event.listen(Engine, "do_connect", misroute)
    try:
        with (
            pytest.raises(RuntimeError, match="database identity differs"),
            prepare_postgres(
                target.render_as_string(hide_password=False),
                ALEMBIC_INI,
                record=stages.append,
                observe_sql=lambda: None,
            ),
        ):
            pytest.fail("a misrouted connection reached schema reset")
    finally:
        event.remove(Engine, "do_connect", misroute)
    assert stages == []
    assert schema_oid(serial_database) == before


def test_connection_failure_never_reaches_reset() -> None:
    stages: list[dict[str, object]] = []

    def refuse_connection(*_args: object) -> None:
        raise OperationalError(None, None, OSError("injected connection failure"))

    event.listen(Engine, "do_connect", refuse_connection)
    try:
        with (
            pytest.raises(OperationalError, match="injected connection failure"),
            prepare_postgres(
                "postgresql+psycopg://localhost/unreachable_test",
                ALEMBIC_INI,
                record=stages.append,
                observe_sql=lambda: None,
            ),
        ):
            pytest.fail("a failed connection reached schema reset")
    finally:
        event.remove(Engine, "do_connect", refuse_connection)
    assert stages == []
