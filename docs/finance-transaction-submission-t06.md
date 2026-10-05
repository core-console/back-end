# Transaction create submissions (T06)

This note records durable implementation facts for
[T06 / Issue #22](https://github.com/core-console/back-end/issues/22). The
[create-submission protocol](finance-create-submission-protocol.md) is the
normative contract; backend-generated OpenAPI documents the wire schemas.

`POST /api/finance/ledgers/{ledgerId}/transactions` accepts the closed v1 command
union `income | expense | internalTransfer`, all under `createFinanceTransaction`.
Its submission headers follow [protocol section 3](finance-create-submission-protocol.md#3-identity-headers-and-user-binding).
The receipt union is `created | rejected`, with a required target Ledger UUID.
Created receipts return HTTP 201 and identify a `transaction` UUID without a
Transaction or Account snapshot. Rejected receipts retain the original business
status/code: missing Account/Category is 404, archived Account/Category is 409,
and mutable date/currency/domain invalidity is 422 `validation_error`. The
operation-specific error schemas require typed `submissionReceipt` for terminal
rejection and distinguish it from unresolved errors and Q29 evidence.

Terminal receipts survive physical delete/reinsert replacement and subsequent
Transaction deletion. Replay returns the original evidence without rerunning
mutable eligibility checks or reconstructing resources. Fetch current
detail/history/balances separately.

The frozen v1 parsers use exact decimal-string Money: CNY/USD scale 2, JPY scale
0, and at most 131,072 significant integer digits for non-zero values. Excess
fractional digits are invalid even when zero; normalization never rounds them
away. Income/Expense require exactly one positive Category Allocation equal to
the complete positive Economic Amount in the same currency. Its Category may
be null; there is no implicit remainder or split expansion. Canonical commands
retain a positive transfer amount between distinct Accounts in one currency and
retain allocation amount/Category/defaults, array order, transfer source and
destination roles, exact civil dates, and normalized UUIDs/notes. Notes are
trimmed; omitted, blank and null match. See the complete field inventory and
normalization rules in [protocol section 4](finance-create-submission-protocol.md#4-versioned-canonical-command).

The existing shared `finance_submissions` binding, owner/key namespace,
admission commit, row serialization, lookup, retention and immutable evidence
machinery cover these three kinds. Migration `20261005_01` widens the closed
command and outcome constraints without backfilling or changing existing
Finance data. Downgrade refuses to remove Transaction enforcement if either
unfinished or terminal Transaction evidence exists; repair forward instead.

Execution takes the submission row lock before invoking the T01 caller-owned
Transaction seams. Income/Expense lock Account then the optional Category;
Internal Transfer retains ascending UUID Account lock order, independent of
source/destination roles. The Transaction, all movements and allocations, and
terminal receipt commit in one outer transaction. Existing public resource
projection and receipt validation run before commit. A savepoint rolls back
tentative Finance work before recognized business rejection evidence is
persisted. Restoring reference eligibility does not change that rejection.
Owner/scope/access failures and unknown failures do not become terminal business
rejection; pre-commit infrastructure or projection failures roll back tentative
work and leave the committed admission unfinished. Lost commit acknowledgement
requires recovery from durable evidence rather than an assumption of rollback.

The 250 ms transaction-local lock timeout applies to admission,
submission-row and Finance locks. It bounds each acquisition, not total request
duration. Generic contention/recovery classification, Q29 proof and lookup rules
are defined in [protocol sections 6-10](finance-create-submission-protocol.md#6-admission-replay-execution-and-contention-ordering),
including [Q29](finance-create-submission-protocol.md#9-q29-definitive-pre-admission-command-validation-rejection)
and [authenticated lookup](finance-create-submission-protocol.md#10-authenticated-lookup).

T06 excludes Balance Adjustment submission commands, receipts, lookup variants
and durable `noChange`. Replacement, deletion, archive and unarchive also receive
no submission protocol in T06.

The existing `execute_create_balance_adjustment` seam accepts signed or zero
balances and checks Account eligibility, date/currency, authoritative derived
balance and Account Nature under the Account lock before calculating its exact
correction delta. A zero delta returns `noChange` without a Transaction; ordinary
create positivity rules must not be applied to Adjustment balance inputs.
A later submission extension must retain submitted `expectedDerivedBalance`,
`expectedAccountNature`, target, Account, date and note, preserve submission-row
locking before the Account lock, and atomically commit terminal evidence with
the created aggregate or explicit `noChange` result. Only Adjustment permits
`noChange`, without resource or rejection fields, and replay must never
recalculate its delta after later balance changes. Adjustment success is HTTP
200 rather than ordinary-create HTTP 201; the shared POST renderer's 201 default
cannot be reused unchanged. Extend the cumulative command, receipt, error,
lookup and persistence constraints coherently under
[protocol sections 2](finance-create-submission-protocol.md#2-covered-operations-and-entry-points)
and [7](finance-create-submission-protocol.md#7-immutable-receipt-and-operation-specific-constraints).
