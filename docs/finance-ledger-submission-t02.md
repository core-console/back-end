# T02 Ledger submission implementation notes

This note describes the delivered T02 baseline. The current cumulative extension
and producer boundary are recorded in [the T04 note](finance-account-category-submission-t04.md).

This note records repository-specific implementation seams for T03/T04. The sole
normative authority is [the create-submission protocol](finance-create-submission-protocol.md).
[Backend #20](https://github.com/core-console/back-end/issues/20) scopes T02 to
Ledger create and known-submission lookup.

## Producer and generated-client seams for T03

The producer artifact is [openapi/openapi.json](../openapi/openapi.json), exported
by the existing exporter. Record the exact delivered producer revision and contract
digest in delivery/handoff evidence. Generated-client responsibilities remain in
[protocol section 15](finance-create-submission-protocol.md#15-responsibility-and-generated-contract-boundaries).

[submission_api.py](../src/core_console/modules/finance/submission_api.py) owns
`createFinanceLedger` and `getFinanceSubmission`; the existing Ledger create
operation ID and HTTP 201 now project `LedgerCreatedReceipt`. Its explicit OpenAPI
header/body declarations accompany delayed runtime body parsing.
[submission_schemas.py](../src/core_console/modules/finance/submission_schemas.py)
owns the receipt, lookup, terminal Problem Details, and Q29 unions. These are the
concrete inputs to T03 generator compatibility checks, including required null
fields and recursive JSON values in attempted-body evidence. OpenAPI error media
types are normalized through the existing OpenAPI exporter. Lookup's `no-store`
middleware also covers dependency, path-validation, and infrastructure errors.

Candidate artifacts under `.agent/reviews/` contain candidate content. Matching
validation/review receipts under `.agent/receipts/` record validation state;
independent Standards and Spec findings are separate review evidence. Their
identity and delivery lifecycle are owned by [the harness](agents/harness.md).

## Database extension seams for T04

[submission_models.py](../src/core_console/modules/finance/submission_models.py)
defines `finance_submissions`, with primary key `(local_user_id, submission_id)`
and the complete canonical command in PostgreSQL JSONB. The named checks
`ck_finance_submissions_command` and `ck_finance_submissions_outcome` admit only
Ledger v1 commands and Ledger created/rejected outcomes. T04 must extend these
checks and operation-specific wire unions together through an additive migration;
the receipt and unfinished-lookup projections also contain Ledger-only fields.
The normative state and outcome rules are in
[sections 5–7](finance-create-submission-protocol.md#5-backend-durable-schema-and-states).

The composite foreign key `fk_finance_submissions_retention_owner` references
Ledger `(id, owner_id)`, backed by `uq_finance_ledgers_id_owner`, without cascading
deletion. The migration's `finance_submission_immutable` trigger rejects command
rebinding and changes to terminal evidence. Later nested-operation constraints
must preserve owner consistency and express the target/retention association
defined in [section 14](finance-create-submission-protocol.md#14-retention-and-version-support).

[Migration 20261003_01](../migrations/versions/20261003_01_add_finance_submissions.py)
adds these constraints without historical submission backfill. Its downgrade takes
an `ACCESS EXCLUSIVE` table lock before checking for evidence and refuses to drop
any populated submission relation. Empty development round trips remain available;
retained evidence requires forward repair, consistent with
[section 16](finance-create-submission-protocol.md#16-migration-cutover-and-rollback).

## Execution and validation seams for T04

[submissions.py](../src/core_console/modules/finance/submissions.py) implements the
transaction flow specified by
[section 6](finance-create-submission-protocol.md#6-admission-replay-execution-and-contention-ordering).
Valid admission and the final Q29 proof acquire the same bounded, transaction-scoped
advisory lock for the owner/key after body parsing, then recheck the binding. Admission
inserts only if it remains absent and commits the binding separately before execution.
Execution obtains `SELECT FOR UPDATE` on the submission before calling T01's
`execute_create_finance_ledger`. Extend operations through their caller-owned execute
seams, preserving existing Finance and Account lock ordering after submission
serialization.

The Finance call runs inside `session.begin_nested()`. A recognized Ledger name
conflict reaches its handler only after the savepoint has rolled back tentative
Finance writes; the outer transaction retains submission serialization while
persisting rejection. Completion uses `session.no_autoflush` while reading database
`clock_timestamp()`, so SQLAlchemy cannot flush a partially assigned terminal tuple.
The complete tuple is flushed and projected into a typed receipt before the final
commit of financial changes and terminal evidence.

Admission, Q29 proof, and execution use transaction-local PostgreSQL
`lock_timeout = 250ms`. This bounds each lock acquisition, including the owner/key
advisory lock, admission uniqueness waits, and execution serialization; it does not
bound total request or execution time. The same transaction-local setting also
applies to later Finance lock acquisitions. SQLSTATE `55P03` maps to the nonterminal
busy response after transaction rollback.

`LedgerCommandV1` is independent of ordinary Finance request schemas. The route
passes a lazy body reader so authorized binding/version checks precede JSON body
interpretation. This separation supports the frozen-validation and exact
attempted-object guards owned by
[section 9](finance-create-submission-protocol.md#9-q29-definitive-pre-admission-command-validation-rejection).
T04 scope authorization must precede evidence disclosure. Lookup and replay
projection changes must continue to follow
[section 10](finance-create-submission-protocol.md#10-authenticated-lookup).

## Regression seams

The authoritative acceptance matrix is
[Testing Decisions](finance-create-submission-protocol.md#acceptance-criteria-and-regression-scenarios),
with Ledger-only scope assigned by backend #20.
[PostgreSQL submission tests](../tests/integration/test_finance_submissions.py)
use barriers for first-admission races and actual held database locks for both
contention paths. Fault injection exercises commit acknowledgement loss, response
loss, post-write failure, savepoint rollback, and access loss after admission.
Populated migration tests preserve existing Finance data and derived results.
[Contract tests](../tests/test_finance_submission_contract.py) exercise schema
unions, error boundaries, and unaffected create contracts. Frontend generator,
browser-journal, and serving-boundary evidence remains downstream work under the
canonical protocol.
