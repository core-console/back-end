# Finance cutover prerequisite assessment (T12)

**Operational qualification: BLOCKED.** No deployment environment, topology,
operations owner, or approved read-only access has been designated. Actual
serving admission state is unknown. T12 issue #25 remains OPEN; the common
activation gate remains CLOSED. This document is an incomplete prerequisite
assessment, not an executable deployment runbook or T12 completion evidence.

The [protocol, section 16](finance-create-submission-protocol.md#16-migration-cutover-and-rollback)
is normative. [T12](https://github.com/core-console/back-end/issues/25) requires
an environment-specific executable procedure validated by real topology and
safe read-only operational checks. [T13](https://github.com/core-console/back-end/issues/26)
owns the later authorized rehearsal and common activation gate. Neither ticket
nor this assessment authorizes production operations.

## Evidence that is available

Repository and issue inspection establishes application prerequisites only:

| Evidence | Established fact | Operational limit |
| --- | --- | --- |
| [Backend README](../README.md), configuration, CLI and application factory | `uv run start` is a reload-enabled development entrypoint; APISIX and Kubernetes infrastructure are deferred. Settings expose no serving pause. Startup does not run migrations. | Local development is not a designated serving environment. No deployed gateway, supervisor, fleet inventory, or deployment owner is identified. |
| [Frontend README](https://github.com/core-console/front-end/blob/main/README.md), consumer API configuration | The consumer documents same-origin `/api`, a development-only Vite proxy, and a production routing requirement through APISIX. | A routing requirement is not deployed routing evidence. Pinned producer/consumer contract alignment must be verified under B6. No hosting target, publication mechanism, cache invalidation mechanism, or live revision is established. |
| [Submission HTTP boundary](../src/core_console/modules/finance/submission_api.py), [submission execution](../src/core_console/modules/finance/submissions.py), [contract tests](../tests/test_finance_submission_contract.py) | All five creates use the shared header/owner/version and durable submission boundary. Missing protocol headers produce `finance_submission_protocol_required`; there is no fallback key. | This checkout does not identify the code running on every serving instance. Authentication/access errors alone cannot prove the protocol guard was reached. |
| Submission execution | `OPEN_ADMISSION_VERSIONS = frozenset({"1"})` is a code constant. Closing a version to new bindings still permits authorized existing-binding replay/resume. Admission and execution have separate commits; lock waits are bounded at 250 ms per acquisition. | A code change on new instances cannot pause old instances or queued/keep-alive requests. Version closure is not a serving-boundary pause, and lock timeouts are not request/drain deadlines. |
| [Health API](../src/core_console/health/api.py), [database resources](../src/core_console/database/resources.py), [session dependency](../src/core_console/database/dependencies.py) | Readiness performs `SELECT 1`; lifespan disposes the engine and sessions are request-scoped. | Readiness proves neither schema version nor universal enforcement. Pool disposal/session cleanup does not establish an empty fleet, drained connections, or the outcome of an earlier transaction. |
| [T11 evidence index](finance-submission-qualification-t11.md) | Existing tests cover populated migration, durable outcomes, replay and downgrade refusal. The index explicitly reserves operational closure/drain/rollback and historical reconciliation for T12/T13. | Protected test PostgreSQL, CI and isolated browser servers are not an actual serving installation. They cannot discharge operational AC43–AC45. |
| Repository/issue inventory | Backend tracked files contain no serving deployment manifests or cutover/drain commands. Parent #18 and T12/T13 supply requirements, not operational topology or ownership. | This is a bounded repository assessment, not proof that no infrastructure exists elsewhere. GitHub publication tooling delivers source/CI; it does not deploy the application. |

## Five-create closure boundary

The deployment owner must identify an effective boundary covering these actual
POST routes, including every ingress, direct backend entry, alternate hostname,
path rewrite and accepted connection that can reach an old executor:

| Operation | Backend route |
| --- | --- |
| `createFinanceLedger` | `/api/finance/ledgers` |
| `createFinanceAccount` | `/api/finance/ledgers/{ledgerId}/accounts` |
| `createFinanceCategory` | `/api/finance/ledgers/{ledgerId}/categories` |
| `createFinanceTransaction` | `/api/finance/ledgers/{ledgerId}/transactions` |
| `createBalanceAdjustment` | `/api/finance/ledgers/{ledgerId}/balance-adjustments` |

**No executable closure mechanism is verified.** Hiding frontend actions,
changing readiness, changing a version constant, or removing an instance from
new load-balancer selection does not establish the required pause and drain.
Existing keyed resume requests can execute Finance work and must be included
in the serving pause. Do not invent a gateway or flag platform to fill this gap.

## Missing operational prerequisites and HUMAN ACTION

The roles below identify responsibilities to assign; they are not known owners.
Each row blocks an environment-specific procedure. Obtain the named facts and
authorized read-only evidence before substituting real commands for these
requirements. Operational commands and credentials require a designated
environment and approved access.

| Blocker | HUMAN ACTION and required evidence | Abort condition |
| --- | --- | --- |
| B1: no qualified environment or owner | Designate the exact target, its purpose and API/frontend origins; name the deployment decision owner, serving operator, database operator, frontend publisher and reconciliation owner. Supply versioned topology/configuration and approved read-only access scope. | Unknown target, owner, or access: stop qualification before operational probing. |
| B2: no verified serving closure | Serving operator identifies the deployed ingress/proxy/supervisor controls, all bypass routes, route matching and propagation behavior. Supply actual read-only inspection commands and configuration evidence showing how closure would cover all five routes throughout the mixed-version interval, including failure/restart behavior. | Any route, bypass, instance or propagation interval unaccounted for: no migration/deployment transition or reopen. |
| B3: no old-executor/connection drain proof | Serving operator supplies complete instance/process/revision inventory, routing membership, keep-alive and queued-request handling, graceful-stop behavior, deadlines, drain observations and restart/autoscaling controls. Establish how old executors are prevented from accepting further work and how every previously accepted request is accounted for. | Timeout, hidden executor, accepted connection or queue with unknown disposition: retain closure and investigate; elapsed time alone is not drain evidence. |
| B4: no deployed PostgreSQL transaction visibility | Database operator identifies the actual primary, application roles/pools/proxies and instance/session attribution, and supplies authorized read-only transaction/lock inspection with adequate visibility (including prepared transactions if applicable). Correlate old requests with committed or rolled-back outcomes and retain unresolved cases. | Incomplete visibility, outstanding old transaction, or ambiguous completion: retain closure. A lost response, missing current session, empty submission lookup or zero observed active requests alone cannot establish rollback. |
| B5: no qualified migration execution environment | Database operator supplies deployed Alembic revision and schema/constraint/trigger inventory, migration role/runner, concurrency exclusion, lock budget and failure handling, and evidence-preserving backup/recovery arrangements. Identify how existing financial data and durable evidence will be compared before/after without historical backfill. | Unknown revision, migration failure, partial/uncertain schema state, lock-budget breach or changed existing data: retain closure and diagnose/repair forward. Do not bypass constraints or reset the database. |
| B6: no fleet/consumer publication verification | Deployment owner supplies immutable backend/frontend artifact identities, generated-contract identity, actual per-instance revision inspection, authorized instance-attributable verification mechanisms and frontend publication/cache behavior. Identify a safe operator-approved check context that reaches each instance's protocol guard without creating financial effects; direct-instance network access is not required if another authorized mechanism establishes instance attribution. | Any unknown/old instance, mismatched consumer, stale publication, or guard result not attributable to the intended instance and access context: no reopen. Actual rehearsal belongs to T13. |
| B7: no enforcement-preserving rollback artifact | Deployment owner supplies a specific compatible rollback pair and its schema/parser/receipt/lookup/enforcement compatibility evidence, or explicitly chooses repair forward with creates remaining closed. Database operator verifies evidence retention; serving operator identifies controls preventing automatic rollback to an unkeyed image. | No demonstrated compatible artifact or inability to preserve closure: rollback is unavailable; repair forward while retaining closure. No destructive downgrade or restoration that loses post-backup keyed evidence. |
| B8: no historical ambiguity inventory or reconciliation owner | Reconciliation owner identifies the pre-protocol request window and affected users/operations, retained request/audit/financial evidence and unresolved cases; document a human disposition for each case before another submission. An empty inventory needs a justified evidentiary basis. | Unknown historical outcome: keep that case unresolved and prevent automatic resubmission/rekeying. Content similarity or absent submission rows cannot resolve it. |

T12 can be reconsidered only after these inputs support concrete commands,
ownership, expected observations and abort points for the designated environment.
Read-only inspection authorization does not authorize changing serving admission,
executing migrations, terminating sessions, publishing artifacts or rollback.
T13 separately needs explicit non-production rehearsal authorization.

## Database and rollback constraints established locally

The explicit migration chain is:

`20260825_01` → `20261003_01` → `20261004_01` → `20261005_01` → `20261006_01`.

The first submission migration adds the owner/key primary key, versioned JSONB
command, terminal tuple checks, owner-consistent retention foreign key and
immutable-update trigger. Later migrations extend command/outcome constraints
for Account/Category, Transaction and Balance Adjustment. They do not manufacture
submission identities for prior resources. Alembic uses validated configuration
through [its environment](../migrations/env.py); application startup does not
migrate. Constraint replacement needs database locks, so its deployed lock and
transaction conditions must be established under B4/B5, not inferred from tests.

The downgrade guards take an exclusive table lock and refuse removal of evidence
that the older schema cannot represent. The base guard refuses to drop a populated
submission table; the extension guards refuse incompatible operation evidence.
An empty-table development downgrade is not permission to reopen an unkeyed
backend. Database guards alone do not preserve application parsers, receipt
replay, lookup or enforcement. No compatible deployment rollback has been
identified. The required fallback is closure plus repair forward, conditional
on an operator first establishing the real closure mechanism.

Local verification references:

- `test_submission_migration_preserves_populated_finance_and_refuses_evidence_erasure`
  in [Ledger persistence tests](../tests/integration/test_finance_submissions.py).
- `test_additive_migration_preserves_old_receipts_positions_and_nested_enforcement`
  in [nested persistence tests](../tests/integration/test_finance_nested_submission_persistence.py).
- `test_t06_migration_preserves_prior_evidence_and_positions_and_blocks_unsafe_downgrade`
  in [Transaction persistence tests](../tests/integration/test_finance_transaction_submission_persistence.py).
- `test_t08_populated_migration_preserves_prior_evidence_and_blocks_unsafe_downgrade`
  in [Adjustment persistence tests](../tests/integration/test_finance_adjustment_submission_persistence.py).

The protected harness may reset only its guarded dedicated `TEST_DATABASE_URL`.
Its fixture is destructive test setup, never an operational evidence command or
migration procedure for a populated serving database. Do not substitute
`DATABASE_URL`, local development or CI for the missing deployment target.

## Historical requests without recoverable submission identity

Retain available evidence and the uncertainty. A request ID or resource ID may
help human investigation but must not be turned into a fabricated submission
key. Name/content similarity does not prove request identity, and legitimate
identical Transactions must survive. Do not backfill, deduplicate, automatically
retry a legacy draft with a new key, or discard durable evidence.

For each genuinely ambiguous pre-protocol request, human reconciliation must
establish its disposition before another submission. A new UUID cannot protect
an earlier unkeyed commit. If outcome evidence is insufficient, record the case
as unresolved; neither missing submission rows nor a lost client response proves
that the financial event did not commit. B8 remains open even when modern keyed
recovery tests pass.

## Acceptance disposition

| Reference | Repository-local assessment | Missing operational evidence |
| --- | --- | --- |
| AC38 | Existing five-operation header enforcement and contract tests are identified. | B2/B3/B6: universal deployed enforcement and safe stale-request handling are unverified. |
| AC43 | Additive populated migration tests and the explicit Alembic migration mechanism are identified. | B1–B6: real closure, drain, transaction resolution and interrupted transition prerequisites are unqualified. No operational AC43 pass. |
| AC44 | Evidence-preserving downgrade guards are identified. | B2/B7: no verified closure or compatible application rollback. No operational AC44 pass. |
| AC45 | No historical backfill in submission migrations; reconciliation constraints are retained. | B8: no historical inventory, owner or case disposition evidence. No operational AC45 pass. |

Protected validation and Standards/Spec review can establish the quality of this
assessment. They cannot supply missing operational evidence, complete T12 or
authorize activation. Preserve T01–T11; leave the T12/T13 issues OPEN and the
common activation gate CLOSED pending the human inputs and separately
authorized work above.
