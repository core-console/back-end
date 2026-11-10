"""Immutable validation execution files and pytest proof verification."""

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

VALIDATION_SCHEMA = "core-console-agent-validation/v2"
HISTORICAL_VALIDATION_SCHEMA = "core-console-agent-validation/v1"
PYTEST_ARGS = (
    "-c",
    "pyproject.toml",
    "-o",
    "addopts=",
    "-ra",
    "--strict-config",
    "--strict-markers",
    "--disable-plugin-autoload",
    "-p",
    "anyio.pytest_plugin",
    "tests",
)


def timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_bytes())
    except OSError, ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def write_new(path: Path, data: dict[str, object]) -> None:
    with path.open("x", encoding="utf-8") as output:
        json.dump(data, output, indent=2, sort_keys=True)
        output.write("\n")


def reserve_execution(output_root: Path) -> tuple[str, Path]:
    execution_id = uuid4().hex
    directory = output_root / "validation" / execution_id
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.mkdir()
    return execution_id, directory


def seal_receipt(path: Path, staging: Path, data: dict[str, object]) -> None:
    """Expose a complete receipt atomically and refuse to replace an existing name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    write_new(staging, data)
    try:
        os.link(staging, path)
    finally:
        staging.unlink()


def execution_directory(output_root: Path, execution_id: object) -> Path | None:
    if isinstance(execution_id, str) and re.fullmatch(r"[0-9a-f]{32}", execution_id):
        return output_root / "validation" / execution_id
    return None


def postgres_target(raw_url: str | None) -> dict[str, object] | None:
    """Record the requested database route without retaining credentials or URL parameters."""
    from sqlalchemy import make_url
    from sqlalchemy.exc import ArgumentError

    if not raw_url:
        return None
    try:
        url = make_url(raw_url)
    except ArgumentError, ValueError:
        return None
    if url.drivername != "postgresql+psycopg" or not url.host or not url.database:
        return None
    return {"host": url.host, "port": url.port or 5432, "database": url.database}


def _node_ids(value: object) -> set[str] | None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return None
    nodes = set(value)
    return nodes if len(nodes) == len(value) else None


def inspect_pytest(start_path: Path) -> tuple[dict[str, object] | None, bool]:
    """Count observed phases and require a complete, successful PostgreSQL run for proof."""
    evidence = read_object(start_path.with_name("pytest.json"))
    if not evidence:
        return None, False
    start = read_object(start_path)
    items = _node_ids(evidence.get("collected"))
    started = _node_ids(evidence.get("started"))
    called = _node_ids(evidence.get("called"))
    reports = evidence.get("reports")
    sql_items = _node_ids(evidence.get("postgresSqlItems"))
    lifecycle = evidence.get("postgresLifecycle")
    deselected = evidence.get("deselected")
    if (
        type(deselected) is not int
        or deselected < 0
        or items is None
        or started is None
        or not started <= items
        or called is None
        or not called <= started
        or not isinstance(reports, dict)
        or not set(reports) <= started
        or sql_items is None
        or not sql_items <= called
        or not isinstance(lifecycle, list)
        or any(not isinstance(stage, dict) for stage in lifecycle)
    ):
        return None, False

    counts = dict.fromkeys(
        (
            "collected",
            "selected",
            "executed",
            "passed",
            "failed",
            "skipped",
            "xfailed",
            "xpassed",
            "neverStarted",
            "neverReachedCall",
            "incomplete",
            "postgresExecuted",
            "postgresPassed",
        ),
        0,
    )
    counts["collected"] = len(items) + deselected
    counts["selected"] = len(items)
    counts["executed"] = len(called)
    counts["neverReachedCall"] = len(items - called)
    counts["neverStarted"] = len(items - started)
    counts["postgresExecuted"] = len(sql_items)
    for item in items:
        phases = reports.get(item, {})
        if not isinstance(phases, dict):
            return None, False
        setup, call, teardown = (phases.get(key) for key in ("setup", "call", "teardown"))
        if (
            setup not in ("passed", "failed", "skipped", "xfailed")
            or teardown not in ("passed", "failed", "skipped")
            or (
                setup == "passed"
                and call not in ("passed", "failed", "skipped", "xfailed", "xpassed")
            )
            or (setup != "passed" and item in called)
            or (call is not None and item not in called)
        ):
            counts["incomplete"] += 1
        if "failed" in (setup, call, teardown):
            counts["failed"] += 1
        elif teardown == "passed" and (setup, call) == ("passed", "passed"):
            counts["passed"] += 1
            counts["postgresPassed"] += item in sql_items
        elif "skipped" in (setup, call, teardown):
            counts["skipped"] += 1
        elif "xfailed" in (setup, call):
            counts["xfailed"] += 1
        elif call == "xpassed" and teardown == "passed":
            counts["xpassed"] += 1

    lifecycle_complete = [stage.get("stage") for stage in lifecycle] == [
        "connected",
        "acquired",
        "initialized",
        "released",
    ]
    if lifecycle_complete:
        connected, _, initialized, released = lifecycle
        heads = initialized.get("migrationHeads")
        lifecycle_complete = (
            isinstance(connected.get("database"), str)
            and bool(connected["database"])
            and connected["database"] == connected.get("expectedDatabase")
            and start.get("postgresTarget")
            == {
                "host": connected.get("targetHost"),
                "port": connected.get("targetPort"),
                "database": connected["database"],
            }
            and all(
                connected.get(key)
                for key in (
                    "databaseOid",
                    "targetHost",
                    "targetPort",
                    "serverVersion",
                    "serverStartedAt",
                    "backendPid",
                )
            )
            and isinstance(initialized.get("schemaOidBefore"), str)
            and isinstance(initialized.get("schemaOidAfter"), str)
            and initialized["schemaOidBefore"] != initialized["schemaOidAfter"]
            and isinstance(heads, list)
            and bool(heads)
            and all(isinstance(head, str) and head for head in heads)
            and heads == released.get("migrationHeads")
        )
    proof = (
        start.get("pytestArguments") == list(PYTEST_ARGS)
        and evidence.get("executionId") == start.get("executionId")
        and evidence.get("invocationSha256") == sha256(start_path)
        and evidence.get("pytestArgs") == list(PYTEST_ARGS)
        and evidence.get("pythonVersion") == start.get("pythonVersion")
        and bool(evidence.get("pytestVersion"))
        and evidence.get("pytestExitCode") == evidence.get("sessionExitCode") == 0
        and deselected == 0
        and counts["failed"] == counts["incomplete"] == 0
        and counts["postgresPassed"] > 0
        and lifecycle_complete
    )
    return {
        "counts": counts,
        "postgresLifecycle": lifecycle,
        "pytestVersion": evidence.get("pytestVersion"),
        "pytestExitCode": evidence.get("pytestExitCode"),
        "sessionExitCode": evidence.get("sessionExitCode"),
        "deselected": evidence.get("deselected"),
        "evidenceSha256": sha256(start_path.with_name("pytest.json")),
    }, proof
