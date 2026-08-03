"""Validate the installed wheel without importing from the repository source tree."""

import sys
import sysconfig
from importlib.metadata import distribution
from pathlib import Path

EXPECTED_ENTRY_POINT = "core_console.cli:start"


def require(condition: bool, message: str) -> None:
    """Raise a focused validation error when an installation invariant fails."""

    if not condition:
        raise RuntimeError(message)


def main() -> None:
    """Validate imports, application construction, and console script metadata."""

    require(
        sys.prefix != sys.base_prefix,
        "installation validation must run in an isolated virtual environment",
    )

    repository_root = Path(__file__).resolve().parents[1]
    working_directory = Path.cwd().resolve()
    require(
        not working_directory.is_relative_to(repository_root),
        f"validation must run outside the repository: {working_directory}",
    )

    import core_console
    from core_console.app import create_app
    from core_console.config import AuthMode, Environment, Settings

    module_file = core_console.__file__
    require(module_file is not None, "core_console has no filesystem location")
    module_path = Path(module_file).resolve()
    site_package_roots = {
        Path(path).resolve()
        for name, path in sysconfig.get_paths().items()
        if name in {"purelib", "platlib"}
    }
    require(
        any(module_path.is_relative_to(root) for root in site_package_roots),
        f"core_console was not imported from this environment's site-packages: {module_path}",
    )
    require(
        "site-packages" in module_path.parts,
        f"core_console path does not contain site-packages: {module_path}",
    )
    require(
        not module_path.is_relative_to(repository_root / "src"),
        f"core_console was imported from repository sources: {module_path}",
    )

    settings = Settings(
        environment=Environment.TEST,
        auth_mode=AuthMode.DEVELOPMENT,
        dev_identity_issuer="https://installation-validation.invalid",
        dev_identity_subject="installed-wheel-validation",
        database_url=None,
        database_connect_timeout_seconds=2.0,
    )
    app = create_app(settings)
    require(app is not None, "create_app() returned None")

    package = distribution("core-console")
    requires_python = package.metadata.get("Requires-Python")
    require(requires_python is not None, "core-console has no Requires-Python metadata")
    start_entry_points = [
        entry_point
        for entry_point in package.entry_points
        if entry_point.group == "console_scripts" and entry_point.name == "start"
    ]
    require(
        len(start_entry_points) == 1,
        f"expected one start console script, found {len(start_entry_points)}",
    )
    start_entry_point = start_entry_points[0]
    require(
        start_entry_point.value == EXPECTED_ENTRY_POINT,
        f"unexpected start console script target: {start_entry_point.value}",
    )
    require(callable(start_entry_point.load()), "start console script target is not callable")

    print(f"Validated core_console from {module_path}")
    print(f"Validated Python {sys.version.split()[0]} against Requires-Python {requires_python}")
    print(f"Validated start = {start_entry_point.value}")


if __name__ == "__main__":
    main()
