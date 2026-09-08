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
to install the project or run the unit tests. The PostgreSQL integration tests
run only when an explicit, dedicated `TEST_DATABASE_URL` is provided.

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
- `GET /api/me`
- `GET /health/live`
- `GET /health/ready`
- `/api/docs` and `/api/openapi.json`

The liveness endpoint reports whether the process can serve requests. Readiness
also checks PostgreSQL. With no `DATABASE_URL`, readiness correctly returns 503;
this does not prevent startup, liveness, or infrastructure-independent APIs.

## Configuration

All environment input is validated by Pydantic Settings:

- `APP_ENV`: `development`, `test`, or `production`
- `AUTH_MODE`: required; currently only `development`
- `DEV_IDENTITY_ISSUER`: required non-blank issuer for the fixed development identity
- `DEV_IDENTITY_SUBJECT`: required non-blank subject for the fixed development identity
- `DATABASE_URL`: optional; when present it must use
  `postgresql+psycopg://`
- `DATABASE_CONNECT_TIMEOUT_SECONDS`: readiness probe timeout, from 0.1 to
  30 seconds

`DATABASE_URL` is held as a secret value and has no implicit host, username,
password, or production fallback.

Development authentication is server-controlled: the backend resolves the fixed
`DEV_IDENTITY_ISSUER` and `DEV_IDENTITY_SUBJECT` to a pre-provisioned PostgreSQL
user. Client headers, query parameters, and cookies cannot select an identity.
Only an active local user can call `GET /api/me`.

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
repositories, CRUD bases, or SQLite fallbacks. The first real migration creates
the users persistence schema; application startup does not run migrations.

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

Contract changes must be reviewed with the backend API implementation and
committed here before consumers synchronize them. The frontend snapshot is a
consumer copy; backend validation and CI never write to the frontend repository.

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

For a candidate handoff, run the repository harness with the candidate's explicit
review base. This is the canonical complete validation route:

```console
uv run --frozen python scripts/agent_harness.py validate --base <sha> --protected
```

The harness runs `scripts/validate.py`, which checks, in order:

1. `uv.lock` consistency
2. Ruff formatting
3. Ruff lint
4. strict mypy
5. pytest
6. OpenAPI drift

It then runs the committed, staged, and unstaged diff checks and writes
snapshot-bound validation evidence. Do not run a second standalone full validation
for the same candidate snapshot.

Run an individual check with the same frozen environment, for example:

```console
uv run --frozen pytest
uv run --frozen ruff check .
```

Most tests use HTTPX's in-process ASGI transport and do not contact real networks
or PostgreSQL. `tests/integration/test_users_postgres.py` uses real PostgreSQL
only through `TEST_DATABASE_URL`; it never falls back to `DATABASE_URL` and
skips locally when no safe test target is available. The test target must be a
dedicated database with a test-marked name. CI supplies a dedicated PostgreSQL
17.6 service database. Coverage can be added when it informs a concrete testing
decision; there is no arbitrary repository-wide threshold.

## Deferred capabilities

Production authentication, authorization, Redis, task queues, brokers, Agent or
LLM SDKs, vector databases, service discovery, Kubernetes clients/manifests,
and distributed-service frameworks are intentionally absent.

The future authentication boundary is documented without a placeholder
implementation: this backend will validate Bearer access tokens forwarded by
APISIX, including issuer, audience, expiry, and required claims. That work starts
only when the real identity and gateway infrastructure contract is available.
