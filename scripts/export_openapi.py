"""Export or verify the deterministic backend-owned OpenAPI contract."""

import argparse
import json
from pathlib import Path

from core_console.app import create_app
from core_console.config import Environment, Settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OPENAPI_PATH = PROJECT_ROOT / "openapi" / "openapi.json"


def rendered_openapi() -> str:
    """Build canonical JSON without consulting local environment configuration."""

    settings = Settings(
        environment=Environment.TEST,
        database_url=None,
        database_connect_timeout_seconds=2.0,
    )
    schema = create_app(settings).openapi()
    return f"{json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True)}\n"


def check_openapi(expected: str) -> int:
    """Return a failing status when the committed contract has drifted."""

    if not OPENAPI_PATH.exists():
        print(f"OpenAPI snapshot is missing: {OPENAPI_PATH}")
        return 1

    actual = OPENAPI_PATH.read_text(encoding="utf-8")
    if actual != expected:
        print(
            "OpenAPI snapshot is out of date. "
            "Run `uv run --frozen python scripts/export_openapi.py`."
        )
        return 1

    print("OpenAPI snapshot is up to date.")
    return 0


def main() -> int:
    """Parse CLI arguments and export or check the schema."""

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare generated output with the committed snapshot",
    )
    args = parser.parse_args()
    rendered = rendered_openapi()

    if args.check:
        return check_openapi(rendered)

    OPENAPI_PATH.parent.mkdir(parents=True, exist_ok=True)
    OPENAPI_PATH.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"Wrote {OPENAPI_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
