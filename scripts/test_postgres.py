"""Ownership and lifecycle for the disposable PostgreSQL integration fixture."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, create_engine, event, make_url, pool, text
from sqlalchemy.engine import Dialect
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.pool import ConnectionPoolEntry, PoolProxiedConnection

# Two-key advisory locks are database-local. Use the same namespace in every checkout.
_LOCK_NAMESPACE = 0x43435453
_OWNER = 1
_CLIENTS = 2
_IDENTITY = """
    SELECT current_database(), oid::text, current_setting('server_version'),
           inet_server_addr()::text, inet_server_port(), pg_postmaster_start_time()::text,
           pg_backend_pid()
    FROM pg_database WHERE datname = current_database()
"""
_CLIENT_AND_OWNER_LOCKED = """
    SELECT count(*) = 2 FROM pg_locks WHERE locktype = 'advisory' AND granted
    AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
    AND classid = %(namespace)s AND objsubid = 2
    AND (
        (objid = %(owner)s AND mode = 'ExclusiveLock' AND pid = %(owner_pid)s)
        OR (objid = %(clients)s AND mode = 'ShareLock' AND pid = pg_backend_pid())
    )
"""


def _lock(connection: Connection, function: str, key: int) -> bool:
    return (
        connection.execute(
            text(f"SELECT pg_catalog.{function}(:namespace, :key)"),
            {"namespace": _LOCK_NAMESPACE, "key": key},
        ).scalar_one()
        is True
    )


def _migration_heads(connection: Connection) -> list[str]:
    return sorted(
        connection.exec_driver_sql("SELECT version_num FROM public.alembic_version").scalars()
    )


@contextmanager
def prepare_postgres(
    guarded_url: str,
    alembic_ini: Path,
    *,
    record: Callable[[dict[str, object]], None],
    observe_sql: Callable[[], None],
) -> Iterator[None]:
    """Hold a database-local lease from before reset through the last fixture cleanup.

    Only accepts a URL already checked by the test-target guard. Controlled SQLAlchemy
    clients take a shared fence so that loss of the owner session cannot allow a reset
    to overlap with a still-connected client from the previous run.
    """
    url = make_url(guarded_url)
    engine = create_engine(url, poolclass=pool.NullPool, connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as owner:
            identity = tuple(owner.exec_driver_sql(_IDENTITY).one())
            database, oid, version, address, port, server_started, pid = identity
            if database != url.database:
                raise RuntimeError("PostgreSQL database identity differs from TEST_DATABASE_URL")
            record(
                {
                    "stage": "connected",
                    "expectedDatabase": url.database,
                    "database": database,
                    "databaseOid": oid,
                    "targetHost": url.host,
                    "targetPort": url.port or 5432,
                    "serverAddress": address,
                    "serverPort": port,
                    "serverVersion": version,
                    "serverStartedAt": server_started,
                    "backendPid": pid,
                }
            )
            if not _lock(owner, "pg_try_advisory_lock", _OWNER) or not _lock(
                owner, "pg_try_advisory_lock", _CLIENTS
            ):
                raise RuntimeError(
                    "exclusive ownership of the disposable PostgreSQL database is busy"
                )
            record({"stage": "acquired"})
            owner.commit()

            config = Config(str(alembic_ini))
            expected_heads = sorted(ScriptDirectory.from_config(config).get_heads())
            with owner.begin():
                schema_oid_before = owner.exec_driver_sql(
                    "SELECT 'public'::regnamespace::oid::text"
                ).scalar_one()
                owner.exec_driver_sql("SET LOCAL search_path TO public")
                owner.exec_driver_sql("DROP SCHEMA public CASCADE")
                owner.exec_driver_sql("CREATE SCHEMA public")
                config.attributes["connection"] = owner
                command.upgrade(config, "head")
                if not expected_heads or _migration_heads(owner) != expected_heads:
                    raise RuntimeError("PostgreSQL migrations did not reach the expected heads")
                schema_oid_after = owner.exec_driver_sql(
                    "SELECT 'public'::regnamespace::oid::text"
                ).scalar_one()
                if schema_oid_before == schema_oid_after:
                    raise RuntimeError("PostgreSQL public schema identity did not change")
            record(
                {
                    "stage": "initialized",
                    "migrationHeads": expected_heads,
                    "schemaOidBefore": schema_oid_before,
                    "schemaOidAfter": schema_oid_after,
                }
            )
            if not _lock(owner, "pg_advisory_unlock", _CLIENTS):
                raise RuntimeError("PostgreSQL reset fence was lost")
            owner.commit()

            enrollment = object()
            lock_parameters = {
                "namespace": _LOCK_NAMESPACE,
                "owner": _OWNER,
                "owner_pid": pid,
                "clients": _CLIENTS,
            }

            def enroll_client(
                dialect: Dialect,
                entry: ConnectionPoolEntry,
                _args: list[Any],
                parameters: dict[str, Any],
            ) -> None:
                # Pool checkout has no Engine URL. Enroll pool entries from the
                # guarded psycopg route before they create their physical sessions.
                if (
                    dialect.name == "postgresql"
                    and dialect.driver == "psycopg"
                    and parameters.get("host") == url.host
                    and parameters.get("dbname") == url.database
                    and str(parameters.get("port", 5432)) == str(url.port or 5432)
                ):
                    record_info = entry.record_info
                    if record_info is None:
                        raise RuntimeError(
                            "PostgreSQL client pool cannot retain ownership metadata"
                        )
                    record_info["protected_enrollment"] = enrollment

            def guard_checkout(
                dbapi_connection: DBAPIConnection,
                entry: ConnectionPoolEntry,
                _proxy: PoolProxiedConnection,
            ) -> None:
                record_info = entry.record_info
                if record_info is None or record_info.get("protected_enrollment") is not enrollment:
                    return

                # Entry info is cleared when its DBAPI session is replaced.
                cursor = dbapi_connection.cursor()
                try:
                    if entry.info.get("protected_owner") != pid:
                        cursor.execute(_IDENTITY)
                        row = cursor.fetchone()
                        if row is None or tuple(row)[:-1] != identity[:-1]:
                            raise RuntimeError("PostgreSQL client reached a different database")
                        cursor.execute(
                            "SELECT pg_try_advisory_lock_shared(%(namespace)s, %(clients)s)",
                            lock_parameters,
                        )
                        if cursor.fetchone() != (True,):
                            raise RuntimeError("PostgreSQL reset is in progress")
                    cursor.execute(_CLIENT_AND_OWNER_LOCKED, lock_parameters)
                    if cursor.fetchone() != (True,):
                        raise RuntimeError("PostgreSQL test database ownership was lost")
                finally:
                    cursor.close()
                # Leave a clean transaction for the caller. The lock belongs to
                # the session and survives rollback and normal pool checkin.
                dbapi_connection.rollback()
                entry.info["protected_owner"] = pid

            def verify_client_enrollment(connection: Connection) -> None:
                if connection.engine.url == url and connection.info.get("protected_owner") != pid:
                    connection.invalidate()
                    raise RuntimeError("PostgreSQL client reached an unguarded connection route")

            def record_sql(
                connection: Connection,
                _cursor: Any,
                _statement: str,
                _parameters: Any,
                _context: Any,
                _executemany: bool,
            ) -> None:
                if connection.engine.url == url and connection.info.get("protected_owner") == pid:
                    observe_sql()

            event.listen(Engine, "do_connect", enroll_client)
            event.listen(pool.Pool, "checkout", guard_checkout)
            event.listen(Engine, "engine_connect", verify_client_enrollment)
            event.listen(Engine, "after_cursor_execute", record_sql)
            try:
                yield
            finally:
                event.remove(Engine, "after_cursor_execute", record_sql)
                event.remove(Engine, "engine_connect", verify_client_enrollment)
                event.remove(pool.Pool, "checkout", guard_checkout)
                event.remove(Engine, "do_connect", enroll_client)
                # SQLAlchemy can reconnect an invalidated Connection. Verify the original
                # backend as well as the heads before recording a successful release.
                if (
                    owner.invalidated
                    or owner.execute(text("SELECT pg_backend_pid()")).scalar_one() != pid
                ):
                    raise RuntimeError("PostgreSQL test database ownership connection was lost")
                if _migration_heads(owner) != expected_heads:
                    raise RuntimeError("PostgreSQL migration state changed during tests")
                if not _lock(owner, "pg_advisory_unlock", _OWNER):
                    raise RuntimeError("PostgreSQL test database ownership was lost")
                record({"stage": "released", "migrationHeads": expected_heads})
    finally:
        engine.dispose()
