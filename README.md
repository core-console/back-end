# Core Console backend

Core Console's main backend is a modular FastAPI application. Frontend business
requests enter here first. The initial system is deliberately a modular monolith:
ordinary CRUD, queries, and business workflows belong here until a concrete need
justifies a specialist service.

## Prerequisites

Python and uv versions are pinned by `.python-version` and
`tool.uv.required-version` in `pyproject.toml`. Install dependencies
deterministically:

```console
uv python install
uv sync
```

No PostgreSQL, Keycloak, Kubernetes cluster, or real network service is required
to install the project or run its tests.

## Development

Copy `.env.example` to `.env` only when local overrides are needed. Never commit
`.env` or real credentials.

Run the application:

```console
uv run start
```

This is the local development entrypoint and enables automatic reload. HTTP
access completion logging is owned by the application Middleware, so this entry
point disables Uvicorn's duplicate access log. This also prevents raw Query
Strings and successful health probes from appearing in Uvicorn access output.
Uvicorn lifecycle and server-error logging remain enabled as the final server
boundary.

Useful endpoints:

- `GET /api/helloWorld`
- `GET /health/live`
- `GET /health/ready`
- `/api/docs` and `/api/openapi.json`

The liveness endpoint reports whether the process can serve requests. Readiness
also checks PostgreSQL. With no `DATABASE_URL`, readiness correctly returns 503;
this does not prevent startup, liveness, or infrastructure-independent APIs.

## Configuration

All environment input is validated by Pydantic Settings:

- `APP_ENV`: `development`, `test`, or `production`
- `DATABASE_URL`: optional; when present it must use
  `postgresql+psycopg://`
- `DATABASE_CONNECT_TIMEOUT_SECONDS`: readiness probe timeout, from 0.1 to
  30 seconds

`DATABASE_URL` is held as a secret value and has no implicit host, username,
password, or production fallback.

## Request correlation and logging

The backend validates an inbound `X-Request-ID` and reuses it only when it is at
most 128 characters and contains ASCII letters, digits, `-`, `_`, `.`, or `:`.
Missing, empty, or invalid values are replaced with `uuid4().hex`. The adopted
identifier is propagated through a `ContextVar` for application logs within that
request and returned in the response `X-Request-ID` header.

Each non-successful-health HTTP request emits one JSON completion event with the
method, matched route template, status, and `duration_ms`. Duration uses a
monotonic clock rather than wall time. Successful liveness and readiness probes
are suppressed; readiness failures remain visible without a traceback for
expected database configuration, connection, or timeout failures.

Application logs currently go only to the process console (`stdout`/`stderr`).
No file logging, log storage system, metrics, tracing, collector, trace ID, or
span ID is configured. When APISIX is introduced, it will become the primary
generator for external request IDs; this backend will continue validating
inbound values and generating a safe fallback.

## Database boundary

SQLAlchemy uses its async engine and session factory with psycopg 3. Creating the
application never opens a database connection. The engine is created during the
application lifespan only when `DATABASE_URL` exists; the first real I/O occurs
when a request or readiness probe needs it. The lifespan disposes the engine on
shutdown.

Request-level `AsyncSession` injection is available in
`core_console.database.dependencies`. There are no fabricated tables, generic
repositories, CRUD bases, SQLite fallbacks, or initial migrations.

Alembic reads the same validated `DATABASE_URL`:

```console
uv run --frozen alembic current
uv run --frozen alembic revision --autogenerate -m "describe the schema change"
uv run --frozen alembic upgrade head
```

Alembic fails clearly when the database URL is absent. Add a migration only with
the real model that requires it.

## API contract

The backend owns the committed `openapi/openapi.json`. Business routes are served
under `/api`; the contract represents that prefix as an OpenAPI server and keeps
the frontend-compatible path `/helloWorld`. Operational health endpoints are not
frontend business APIs and are excluded from this contract.

Export the deterministic contract:

```console
uv run --frozen python scripts/export_openapi.py
```

Check for drift without changing files:

```console
uv run --frozen python scripts/export_openapi.py --check
```

The JSON export is sorted, has a fixed newline, and contains no timestamps,
machine paths, or random values.

## Quality checks

Run the complete local validation entrypoint after syncing:

```console
uv run --frozen python scripts/validate.py
```

It checks, in order:

1. `uv.lock` consistency
2. Ruff formatting
3. Ruff lint
4. strict mypy
5. pytest
6. OpenAPI drift

Run an individual check with the same frozen environment, for example:

```console
uv run --frozen pytest
uv run --frozen ruff check .
```

Tests use HTTPX's in-process ASGI transport. They do not contact real networks or
PostgreSQL. Coverage can be added when it informs a concrete testing decision;
there is no arbitrary repository-wide threshold.

## Deferred capabilities

Authentication, authorization, Redis, task queues, brokers, Agent or LLM SDKs,
vector databases, service discovery, Kubernetes clients/manifests, and
distributed-service frameworks are intentionally absent.

The future authentication boundary is documented without a placeholder
implementation: this backend will validate Bearer access tokens forwarded by
APISIX, including issuer, audience, expiry, and required claims. That work starts
only when the real identity and gateway infrastructure contract is available.
