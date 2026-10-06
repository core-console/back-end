# Balance Adjustment create submissions (T08)

This note records durable implementation facts for
[T08 / Issue #23](https://github.com/core-console/back-end/issues/23).
The [create-submission protocol](finance-create-submission-protocol.md) owns the
normative behavior; backend-generated [OpenAPI](../openapi/openapi.json) owns the wire
schemas. The browser adapter must consume the exact reviewed producer revision
after separately authorized backend delivery and before frontend synchronization.

`POST /api/finance/ledgers/{ledgerId}/balance-adjustments` retains operation ID
`createBalanceAdjustment` and requires the three submission headers in
[protocol section 3](finance-create-submission-protocol.md#3-identity-headers-and-user-binding).
Created and `noChange` receipts both return HTTP 200 on initial execution and
identical replay. A `created` receipt includes an `outcome.resource` with
`type: transaction` and the created Transaction `id`. The `noChange` outcome
contains only `kind: noChange`, with no resource or rejection fields, and creates
no Transaction. Neither success variant contains a balance snapshot.
Current detail, history and Account balances must be fetched separately.

The original Account, target Ledger, target balance, expected derived balance,
expected Account Nature, Transaction Date and normalized note remain immutable
command identity. Retry must resend the submitted values, including stale
expected state; it cannot refresh expectations or calculate another delta.
Exact Money uses frozen v1 currency scales and the existing durable range.
Balance inputs permit signed and zero values for both Asset and Liability
Accounts. Negative zero and valid scale/leading-zero equivalents canonicalize
without rounding invalid precision. Immutable invalidity uses the existing
guarded [Q29 path](finance-create-submission-protocol.md#9-q29-definitive-pre-admission-command-validation-rejection);
Account currency, eligibility, tracking date and authoritative state checks are
mutable execution checks and cannot supply Q29 proof.

Execution uses the shared admission commit, submission row serialization and
caller-owned Adjustment seam. It locks the Account before validating its
authoritative balance and Nature, then derives the exact correction. A zero
delta creates no Transaction, Movement or Allocation. Its explicit `noChange`
result and terminal evidence commit together. A non-zero correction and its
receipt also share one outer commit. Public result projection and receipt
validation precede that commit. Recognized business failures roll back tentative
work through the existing savepoint before their rejection evidence commits;
unknown failures before terminal commit leave the committed admission unfinished.
Lost commit acknowledgement or response loss leaves the client uncertain; lookup
or identical retry observes any durable terminal outcome.

Terminal business rejection preserves 404 `finance_account_not_found`,
409 `finance_account_archived`, `account_balance_changed` and
`finance_account_semantics_changed`, or 422 `validation_error`, with required
typed `submissionReceipt`. These statuses are distinct from unresolved protocol,
access, infrastructure and generic validation errors. Restoring eligibility
cannot change an original rejection. Terminal replay never recalculates,
including `noChange` after later balance changes, or created evidence after
Adjustment replacement/removal or Transaction deletion.

The existing known-ID lookup now includes Adjustment unfinished and all three
terminal variants. It returns HTTP 200 even for terminal rejection, omits command
content, enforces owner/scope access, and uses `Cache-Control: no-store` on success
and errors. Lookup does not execute; absence remains unresolved. Correlate its
receipt against the original key, version, operation and target Ledger before
resolving browser recovery. Refresh failure cannot reopen a terminal create.

Migration `20261006_01` widens the cumulative command/outcome constraints without
backfilling or changing financial data. It retains prior create enforcement,
the common owner/key namespace, owner-consistent Ledger retention and immutable
evidence. Downgrade refuses while any unfinished or terminal Adjustment binding
exists; preserve enforcement and repair forward. Replacement, deletion, archive
and unarchive stay outside the submission protocol. Browser Adjustment recovery
and common integration/cutover qualification remain separate work; this backend
slice does not authorize production activation.
