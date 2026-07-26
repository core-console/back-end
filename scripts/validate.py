"""Run the complete local and CI validation gate."""

import subprocess
import sys
from collections.abc import Sequence

CHECKS: tuple[tuple[str, Sequence[str]], ...] = (
    ("uv lock", ("uv", "lock", "--check")),
    ("Ruff format", ("ruff", "format", "--check", ".")),
    ("Ruff lint", ("ruff", "check", ".")),
    ("mypy", ("mypy",)),
    ("pytest", ("pytest",)),
    (
        "OpenAPI drift",
        (sys.executable, "scripts/export_openapi.py", "--check"),
    ),
)


def main() -> int:
    """Stop at the first failed quality gate."""

    for label, command in CHECKS:
        print(f"==> {label}", flush=True)
        result = subprocess.run(command, check=False)
        if result.returncode != 0:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
