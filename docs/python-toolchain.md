# Python toolchain assessment

Assessed against upstream stable releases on 2026-10-02. The modernization keeps
the existing uv project, `src` layout, FastAPI factory/lifespan, explicit Psycopg
dialect, migrations, and snapshot-bound validation/publication harness.

## Runtime and dependencies

Python 3.14.8 is the newest stable release currently listed by
[Python.org](https://www.python.org/downloads/). Python 3.15 is still listed as a
prerelease; a scheduled release date alone does not establish availability.
`.python-version` pins the patch used locally and in CI. `requires-python`
deliberately retains `>=3.14,<3.15`: this backend is an application with a tested
runtime, and does not claim compatibility with an unvalidated future minor.
[PyPA advises considering Python upper bounds carefully](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/#requires-python);
the cap here is a support boundary, not a library dependency workaround.

Direct runtime versions are Alembic 1.20.0, FastAPI 0.142.2, Psycopg 3.3.6,
Pydantic 2.13.5, Pydantic Settings 2.15.0, SQLAlchemy 2.1.2, Starlette 1.7.0, and
Uvicorn 0.54.0. Development uses AnyIO 4.15.1, HTTPX 0.28.1, mypy 2.4.0,
pytest 9.1.1, and Ruff 0.16.10. Versions were checked against each project's
[PyPI metadata](https://pypi.org/) and the universal lock was regenerated with
uv 0.12.22. Exact dependency pins remain intentional for this application;
the lock records the complete resolved graph, hashes, and platform markers.

Starlette is now a direct requirement because application middleware and Problem
Details handling import its public APIs. AnyIO remains a development requirement
for its pytest plugin. SQLAlchemy's `asyncio` extra remains required for greenlet;
[SQLAlchemy 2.1 no longer installs it by default](https://docs.sqlalchemy.org/en/21/changelog/migration_21.html#asyncio-greenlet-dependency-no-longer-installs-by-default).
The 2.1 migration guide also identifies unconditional session autoflush and
PostgreSQL type handling as behavior changes; the protected database suite is
the compatibility gate for the existing transaction and migration semantics.

CI retains PostgreSQL 17 and updates its patch to 17.11, the
[current supported minor](https://www.postgresql.org/support/versioning/).
Changing the database major would be a separate database upgrade rather than a
Python toolchain requirement. The explicit test-target guard remains mandatory.

## Configuration and packaging conventions

- `[project]`, `[build-system]`, and `[project.scripts]` already follow
  [PyPA's pyproject conventions](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/).
  Repository and issue URLs now use standard metadata labels. License and author
  metadata are not invented; no license decision was authorized by this work.
- Development dependencies already use standardized
  [dependency groups](https://docs.astral.sh/uv/concepts/projects/dependencies/#dependency-groups),
  rather than deprecated `tool.uv.dev-dependencies` or runtime extras. No parallel
  requirements, Poetry, pip-tools, tox, or type checker is introduced.
- pytest now uses native `[tool.pytest]` TOML, supported since
  [pytest 9](https://docs.pytest.org/en/stable/reference/customize.html#pyproject-toml).
  The superseded INI-compatibility table is removed; options and test discovery
  remain the same. A separate `pytest.toml` is unnecessary.
- Ruff keeps its stable rule set and formatting policy; it
  [infers the minimum Python version from `requires-python`](https://docs.astral.sh/ruff/configuration/#inferring-the-python-version).
  mypy explicitly targets Python 3.14 and remains the sole type checker.
- Alembic's supported `alembic.ini` retains its Python logging configuration and
  existing migration-runner contract. Moving its project settings into
  `pyproject.toml` would still leave logging configuration and alter the tested
  runner; no obsolete Alembic option requires that migration.
- Hatchling 1.32.4 remains a supported PEP 517 backend. uv_build is an available
  [alternative](https://docs.astral.sh/uv/concepts/build-backend/), not a requirement
  of a uv-managed project. Switching would change distribution file-selection
  defaults without a demonstrated need. Hatchling's build dependencies are
  pinned with uv's
  [build constraints](https://docs.astral.sh/uv/reference/settings/#build-constraint-dependencies)
  because build isolation resolves separately from `uv.lock`.
- CI now uses uv's default
  [source-distribution-to-wheel build](https://docs.astral.sh/uv/concepts/projects/build/)
  with `--no-sources`, and installs hash-verified locked runtime dependencies
  before installing the wheel with `--no-deps`. The existing isolated installation
  check exercises package imports, application construction, and the `start`
  console entry point without the editable checkout or development dependencies.

No preview lint rules, free-threaded interpreter, dynamic versioning, package
publication metadata policy, or application architecture change is needed to
meet current Python/PyPA/uv conventions. Existing explicit loop factories remain
necessary for Psycopg on Windows; they avoid deprecated asyncio policy APIs.

## Verification and security

The authoritative acceptance path is the protected repository harness described
in [`agents/harness.md`](agents/harness.md). It retains lock consistency,
format/lint, strict typing, the full test suite, deterministic OpenAPI drift,
database safeguards, and diff checks. Canonical snapshot evidence lives in the
ignored `.agent/` directory. Packaging is checked separately using the CI build
and isolated installation procedure.

Use the existing uv command `uv audit --locked` for online advisory and adverse
package-status checks, including all dependency groups. Audit both Windows and
Linux resolutions and the pinned isolated build toolchain; do not add another
audit package or make ordinary tests depend on a public service. An audit is
evidence about currently published advisories, not a guarantee about unknown
vulnerabilities. In uv 0.12.22 the audit command and JSON output are experimental;
they are used explicitly for maintenance, without enabling project-wide preview
features or making their output schema a validation contract. No advisory
suppression is part of this modernization.

The 2026-10-02 audits returned zero vulnerabilities and zero adverse statuses for
39 resolved packages on each of Windows and Linux, plus seven packages in the
separate uv/Hatchling build-toolchain audit. The JSON results are retained as
`.agent/audit-windows.json`, `.agent/audit-linux.json`, and
`.agent/audit-build.json` for this candidate.
