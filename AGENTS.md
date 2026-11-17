# Core Console backend engineering rules

## Architecture

- Keep the application a modular monolith until a measured requirement warrants
  a specialist service.
- Organize business code by module. Do not create global dumping grounds named
  `controllers`, `services`, `repositories`, `models`, or `utils`.
- Keep application construction in the factory and external-resource ownership
  in the lifespan. Imports must not connect to infrastructure.
- Put asynchronous code at real I/O boundaries. Do not make pure business logic
  async.
- Frontend business API requests enter this backend first. Health and other
  operational endpoints are not frontend business APIs.

## API contract

- The backend-generated, committed OpenAPI document is authoritative.
- Preserve existing paths, operation IDs, status codes, and response structures
  unless a deliberate contract change is reviewed with its consumers.
- Export OpenAPI deterministically and check for drift when the API contract or
  export process changes. CI also checks drift.
- Return API errors as `application/problem+json` Problem Details with stable
  machine-readable codes.

## Configuration and infrastructure

- Validate all environment configuration through Pydantic Settings.
- Never commit secrets or add implicit production credentials, hosts, or
  passwords.
- PostgreSQL is the only persistence target. Do not add a SQLite fallback.
- Do not connect to PostgreSQL at import time or make startup depend on it.
- Add database models and migrations only for real business requirements. Do not
  add example tables, empty migrations, generic CRUD bases, or repository bases.
- Do not add placeholder authentication. When implemented, validate the APISIX
  forwarded Bearer token's issuer, audience, expiry, and required claims.

## Dependencies and quality

- Manage Python and dependencies with uv and keep `uv.lock` synchronized.
- Add only directly used dependencies and only the minimum justified extras.
- Ruff owns formatting and linting. mypy is the sole static type checker.
- Ordinary unit and foundational tests must not require real public networks,
  PostgreSQL, Keycloak, or Kubernetes. Explicitly marked PostgreSQL integration
  tests may use a dedicated local or CI PostgreSQL via `TEST_DATABASE_URL`, but
  must never fall back to or clean `DATABASE_URL`; Keycloak, Kubernetes, and
  real public networks remain outside current integration-test dependencies.
- For ordinary changes, understand the relevant implementation and specification,
  then run focused tests and static checks appropriate to the change. Before
  publishing, check the final source content once; CI runs the complete suite.
- For changes to database ownership, credentials, validation infrastructure, or
  other sensitive infrastructure, run protected validation and relevant focused
  recovery and regression tests. See `docs/agents/harness.md`.
- Follow Conventional Commits and keep each commit to one logical concern.

## Change approval boundary

- Do not commit or push unless the user explicitly approves it in the current conversation.
- Use normal Git commit and push for ordinary publication, then verify GitHub CI
  for the published commit. Committing unchanged, already-checked content does
  not require repeating local validation. The protected publication harness is
  available when its additional evidence is wanted.
- Close an issue only when explicitly authorized and after its delivery and
  acceptance requirements are satisfied.
- Repository instructions take precedence over conflicting workflow defaults.
- Do not expand the approved scope while addressing review findings.

## Agent skills

Protected validation and the optional publication harness are documented in
`docs/agents/harness.md`.

### Issue tracker

Issues and PRDs are tracked in GitHub Issues. See `docs/agents/issue-tracker.md`.

### Domain docs

This repository uses a single-context domain-doc layout. See `docs/agents/domain.md`.
