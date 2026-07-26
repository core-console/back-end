# Core Console backend

Core Console's main backend starts as a modular FastAPI monolith. Application
construction is isolated in a factory, while external resources are owned by its
lifespan.

## Prerequisites

- CPython 3.14.6
- uv 0.11.32

Install the project and all development tools from the lock file:

```console
uv python install
uv sync --locked --all-groups
```

No PostgreSQL or other external infrastructure is required to import, install,
or test the project.

## Development

Copy `.env.example` to `.env` only for local overrides. Never commit `.env` or
real credentials.

Run the application:

```console
uv run --frozen uvicorn core_console.app:create_app --factory --reload
```

Configuration is validated by Pydantic Settings:

- `APP_ENV`: `development`, `test`, or `production`
- `DATABASE_URL`: optional and restricted to `postgresql+psycopg://`
- `DATABASE_CONNECT_TIMEOUT_SECONDS`: from 0.1 to 30 seconds

`DATABASE_URL` is stored as a secret and has no implicit host, username,
password, or production fallback.

## Database boundary

SQLAlchemy uses an async engine and session factory with psycopg 3. Creating the
application does not connect to PostgreSQL. The lifespan creates an Engine only
when `DATABASE_URL` is configured and disposes it during shutdown.

There are no fabricated tables, generic repositories, CRUD bases, SQLite
fallbacks, or initial migrations. Alembic uses the same validated database URL:

```console
uv run --frozen alembic current
uv run --frozen alembic revision --autogenerate -m "describe the schema change"
uv run --frozen alembic upgrade head
```

Alembic fails clearly when the database URL is absent.

## Quality checks

Run the complete foundation validation:

```console
uv run --frozen python scripts/validate.py
```

It checks lock-file consistency, Ruff formatting and linting, strict mypy, and
pytest. Tests do not contact real networks or PostgreSQL.
