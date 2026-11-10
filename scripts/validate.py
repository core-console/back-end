"""Run the complete local and CI validation gate."""

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.database_diagnostics import DatabaseRedactor

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

    redact = DatabaseRedactor(Path.cwd())
    for label, command in CHECKS:
        print(f"==> {label}", flush=True)
        result = subprocess.run(command, check=False, capture_output=True)
        sys.stdout.buffer.write(redact.bytes(result.stdout))
        sys.stdout.buffer.flush()
        sys.stderr.buffer.write(redact.bytes(result.stderr))
        sys.stderr.buffer.flush()
        if result.returncode != 0:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
