"""Run the fixed protected pytest profile and capture pytest and fixture observations."""

import hashlib
import json
import os
import platform
import sys
from collections.abc import Generator
from pathlib import Path

import pytest

from scripts.pytest_database_diagnostics import DIAGNOSTICS, ReportDiagnostics
from scripts.validation_evidence import PYTEST_ARGS

OBSERVER = pytest.StashKey["ExecutionObserver"]()


class ExecutionObserver:
    """Keep per-item phase outcomes separate from PostgreSQL lifecycle observations."""

    def __init__(self) -> None:
        self.diagnostics = ReportDiagnostics(Path.cwd())
        self.items: list[str] = []
        self.deselected = 0
        self.started: set[str] = set()
        self.called: set[str] = set()
        self.reports: dict[str, dict[str, str]] = {}
        self.postgres: list[dict[str, object]] = []
        self.sql_items: set[str] = set()
        self.active_item: str | None = None
        self.exit_status: int | None = None

    @pytest.hookimpl(trylast=True)
    def pytest_configure(self, config: pytest.Config) -> None:
        config.stash[OBSERVER] = self
        self.diagnostics = config.stash[DIAGNOSTICS]

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.items = [self.diagnostics.nodeid(item.nodeid) for item in session.items]

    def pytest_deselected(self, items: list[pytest.Item]) -> None:
        self.deselected += len(items)

    def pytest_runtest_logstart(self, nodeid: str) -> None:
        self.started.add(self.diagnostics.nodeid(nodeid))

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_call(self, item: pytest.Item) -> Generator[None, object, object]:
        self.active_item = self.diagnostics.nodeid(item.nodeid)
        self.called.add(self.active_item)
        try:
            return (yield)
        finally:
            self.active_item = None

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        phases = self.reports.setdefault(report.nodeid, {})
        phases[report.when] = (
            ("xfailed" if report.skipped else "xpassed")
            if hasattr(report, "wasxfail")
            else report.outcome
        )

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        self.exit_status = int(exitstatus)

    def record_sql(self) -> None:
        if self.active_item is not None:
            self.sql_items.add(self.active_item)


def main(start_path: Path) -> int:
    """Accept only the harness's execution manifest as a command-line input."""
    start_bytes = start_path.read_bytes()
    manifest = json.loads(start_bytes)
    for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        if os.environ.get(name, "").strip():
            print(f"Protected pytest rejects external {name}.", file=sys.stderr)
            return 2
    for name in tuple(os.environ):
        if name.upper().startswith(("PYTEST_", "_PYTEST_")):
            del os.environ[name]
    os.environ["CI"] = "true"

    observer = ExecutionObserver()
    status = int(
        pytest.main(list(PYTEST_ARGS), plugins=["scripts.pytest_database_diagnostics", observer])
    )
    redact = observer.diagnostics.redact
    evidence = {
        "executionId": manifest["executionId"],
        "invocationSha256": hashlib.sha256(start_bytes).hexdigest(),
        "pytestArgs": list(PYTEST_ARGS),
        "pythonVersion": platform.python_version(),
        "pytestVersion": pytest.__version__,
        "pytestExitCode": status,
        "sessionExitCode": observer.exit_status,
        "collected": observer.items,
        "started": sorted(observer.started),
        "called": sorted(observer.called),
        "deselected": observer.deselected,
        "reports": observer.reports,
        "postgresLifecycle": [
            {
                key: redact(value)
                if key in {"database", "expectedDatabase", "targetHost", "serverAddress"}
                and isinstance(value, str)
                else value
                for key, value in stage.items()
            }
            for stage in observer.postgres
        ],
        "postgresSqlItems": sorted(observer.sql_items),
    }
    with start_path.with_name("pytest.json").open("x", encoding="utf-8") as output:
        json.dump(evidence, output, indent=2, sort_keys=True)
    return status
