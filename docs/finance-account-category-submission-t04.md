# T04 Account and Category submission implementation notes

This note records implementation facts for T05 and later Transaction work.
The sole normative authority remains the
[create-submission protocol](finance-create-submission-protocol.md).
[Backend #21](https://github.com/core-console/back-end/issues/21) scopes this slice.
T04 extends the shared Ledger submission machinery to Account and Category creates.
Frontend integration, Transaction and Balance Adjustment submissions, and production
cutover belong to separate slices.

## Cumulative producer boundary

The backend-generated [OpenAPI document](../openapi/openapi.json) describes the
cumulative Ledger, Account, and Category contract. The protocol owns
[identity and headers](finance-create-submission-protocol.md#3-identity-headers-and-user-binding)
and [receipt constraints](finance-create-submission-protocol.md#7-immutable-receipt-and-operation-specific-constraints).
Receipts carry immutable evidence; current resource state comes from ordinary reads.

Account HTTP 422 has three distinct schema branches: nonterminal failures,
correlated Q29 command-validation proof, and terminal recognized Account business
rejection with `submissionReceipt`. Category HTTP 409 distinguishes nonterminal
conflicts from terminal name rejection. These branches cannot carry both receipt
and Q29 evidence. Lookup keeps its outer `state` discriminator; unfinished metadata
and terminal receipts now discriminate the three operations, with UUID scopes for
nested creates and a required null scope for Ledger creation. Generated consumers
must handle this cumulative lookup union and correlate owner, submission ID, version,
operation, scope, and outcome/resource type. The Account request's nested Money schema
is inline so all references resolve in the complete OpenAPI document. See
[response classification](finance-create-submission-protocol.md#8-response-classification-and-protocol-errors),
[Q29 correlation](finance-create-submission-protocol.md#9-q29-definitive-pre-admission-command-validation-rejection),
and [lookup](finance-create-submission-protocol.md#10-authenticated-lookup).

T05 must pin the exact reviewed and subsequently delivered producer revision and
OpenAPI digest under the protocol's
[generated-contract boundary](finance-create-submission-protocol.md#15-responsibility-and-generated-contract-boundaries).
Generator compatibility and browser behavior remain T05 responsibilities.

## Exact command and execution facts

[submission_commands.py](../src/core_console/modules/finance/submission_commands.py)
owns frozen v1 validation independently of mutable resource request schemas.
The authoritative field inventory, currency scales, durable range, and normalization
rules are in the protocol's
[versioned canonical command](finance-create-submission-protocol.md#4-versioned-canonical-command).
Frozen v1 validation checks the original Money string before canonicalization, so
excess precision remains invalid even when its extra digits are zeros. Valid amounts
use exact decimal/string processing while normalizing leading zeros, negative zero,
and currency scale. Opening Balance retains its signed account-relative position.
Changing mutable resource validators or the currency catalog cannot relax v1 rules.

[submissions.py](../src/core_console/modules/finance/submissions.py) owns one shared
admission, replay, execution, and lookup flow. Current requested scope authorization
precedes binding/version disclosure and parsing, and is rechecked at admission/Q29
serialization and unfinished execution. The owner/key namespace is shared across
operations and Ledgers; the owner header is an assertion, not an authorization
credential. Admission and Q29 use the same transaction advisory lock and binding
recheck. Admission commits before execution locks the submission row. Caller-owned
Finance execute seams acquire their locks afterward; T06 must preserve existing
Account UUID ordering and Account-before-Category ordering where applicable. The
transaction-local 250ms lock budget bounds each lock wait, not the whole request.

Account creation and its Opening Balance position commit with terminal evidence;
Opening Balance creates no Transaction or Movement. Accounts have no name uniqueness
rule: a fresh key can create an intentionally identical Account. Category uniqueness
remains case-folded within a Ledger, including archived Categories. Terminal name
rejection replays after the conflicting Category is renamed. Recognized business
rejection is persisted only after savepoint rollback; access, busy, infrastructure,
and unknown errors preserve unfinished evidence. All terminal projections precede
commit; `session.no_autoflush` prevents a partially assigned terminal tuple from
flushing during the timestamp read. Commit acknowledgement loss is recovered by
reading actual database state. Generic errors and lookup absence do not prove
non-admission. Terminal replay returns retained evidence without rerunning mutable
business validation. These seams implement the protocol's
[admission and execution ordering](finance-create-submission-protocol.md#6-admission-replay-execution-and-contention-ordering).

## Persistence and retention

[Migration 20261004_01](../migrations/versions/20261004_01_extend_finance_submissions.py)
replaces the existing named command/outcome checks additively, without another table,
namespace, worker, or backfill. Its SQL is frozen in the migration rather than imported
from evolving application definitions. Closed canonical-body and operation/outcome
checks reject incomplete tuples and JSON-null/missing-field loopholes.

Nested bindings retain their target Ledger from admission through every terminal
outcome, using T02's owner-consistent composite foreign key. That foreign key also
prevents changing the Ledger owner underneath retained evidence. Lookup and replay
authorize against this retained scope before disclosing evidence. Created Account and
Category IDs are receipt evidence, with no resource foreign key that could prevent
deletion or cascade away the receipt. The existing immutable-evidence trigger remains
in force. Downgrade to T02 refuses while any nested binding remains, including
unfinished evidence; Ledger-only retained evidence can survive a development round
trip. Earlier downgrade continues to refuse erasing any submission evidence. Later
schema extensions must preserve old Ledger evidence and evolve closed command checks,
outcome checks, and typed unions together within the shared table and namespace. See
[durable schema](finance-create-submission-protocol.md#5-backend-durable-schema-and-states)
and [retention](finance-create-submission-protocol.md#14-retention-and-version-support).

## Verification seams

[Nested submission regressions](../tests/integration/test_finance_nested_submissions.py)
exercise contention, rollback/recovery, exact Money/Q29, authorization, and replay.
[Persistence regressions](../tests/integration/test_finance_nested_submission_persistence.py)
exercise closed tuples, retention, populated upgrades, and guarded downgrade.
[Contract regressions](../tests/test_finance_submission_contract.py) exercise cumulative
operation-specific evidence and schema references. Existing Finance lifecycle tests
prepare keyed Account/Category creates and read current resource projections separately.
