# Finance v2 T12 backend producer

Authority: immutable specification `e206bdd5af48a3f6b3b9b525c27fa6178220bdc7`,
sections 4.7 and 7.1–7.2; scoped AC32, AC33, AC35 and AC40. Canonical tracker:
[back-end#36](https://github.com/core-console/back-end/issues/36).

This producer corrects original Advance facts only before downstream settlement
activity. Any cash Allocation, forgiveness or credit application against any
Obligation of the Advance blocks both preview and confirmation with
`finance_advance_correction_ineligible`. It does not correct/cancel Settlements,
release Allocations, create or consume credit, process refunds, renegotiate debt,
or cancel an Advance. Unrelated retained credit and Settlements remain unchanged.

## Contract

All paths below are relative to `/api/finance/ledgers/{ledgerId}`:

| Method and path | Operation |
| --- | --- |
| POST `/advance-corrections/preview` | `previewFinanceAdvanceCorrection` |
| POST `/advance-corrections` | `createFinanceAdvanceCorrection` |
| GET `/advance-corrections/{correctionId}` | `getFinanceAdvanceCorrection` |
| GET `/advances/{advanceId}/corrections` | `listFinanceAdvanceCorrections` |

Preview accepts `advanceId`, a nonblank `reason` of at most 500 Unicode code
points, and the complete corrected funding Account, financial date, total,
owner share, participant shares and optional note. Money remains an exact decimal
string. Total is positive; owner share is non-negative; participant shares are
positive, unique and sum with owner share to exactly the total in the existing
Advance currency. An empty participant share list means a fully owner-funded
purchase, with no invented participant debt. Zero funding/cancellation is rejected.

The preview exposes complete original/effective Advance projections, linked
financial effects before/after, each affected Account balance before/after, and
whether the funded amount or funding Account changed. Reducing participant
shares on truthful funding increases the explicitly entered owner/other shares;
it never silently reduces actual funding. The confirmed financial date restates
the original event; the separate Correction confirmation time records when its
facts were repaired. Future-dated recorded effects apply immediately.

Confirmation sends the exact intent plus `previewToken` with
`Finance-Command-Version: 1`, a UUIDv4 `Idempotency-Key` and the prepared
`Finance-Submission-Owner`. This is a new operation with its own version 1
grammar, not a reinterpretation of earlier Advance or Settlement versions.
The created resource type is `advanceCorrection`.

The existing `/api/finance/submissions/{submissionId}` protocol provides retained
unfinished/terminal lookup and exact-command retry. All Correction, domain,
financial and terminal effects commit atomically. Stale plans are terminally
rejected with `finance_advance_correction_preview_stale`; refresh the complete
preview and explicitly reconfirm under a new key. An old terminal key never
accepts changed content. Generic errors, absent lookup and failed resource
refresh are not proof that a Correction failed to commit. Retain the prepared
command until correlated terminal evidence resolves it.

## Effective facts and retained history

Advance identity and original confirmation time survive. Existing Obligation
identities/receivable Accounts survive, including zero-principal records for
removed attribution. New attribution creates a distinct managed receivable.
Restoring a removed archived Participant as a debtor requires active status;
existing archived attribution remains correctable. Current Advance reads use
the already-supported shared projection after Correction, including when the
original creation used the frozen pure-Advance grammar.

The coordinated workflow restates effective linked Transfers and owner Expense
at the corrected original date. Superseded financial effects may be removed or
replaced together; their complete before/after representation remains in the
Correction history. This is not a global append-only Finance ledger. The original
Advance remains and is never hard-deleted. Existing creation receipts remain
unchanged and continue to identify that Advance. History is ordered by immutable
per-Advance revision, retains the actor/reason, and embeds the original and each
intermediate set of facts without deriving it from mutable current records.

Asset funding decreases (Liability funding increases) by corrected total;
receivables equal corrected participant principals; only owner share is Expense.
Cash-paid, forgiven and credit-applied remain zero. No Income, hidden repayment,
credit consumption, rounding or new Transaction kind is introduced. Accounts
and Participants are locked in their established order before checking current
eligibility and the complete preview token. Linked ordinary Transaction
replacement/deletion, manual managed-Account entries, Account edits and Balance
Adjustments retain their existing protections.

Migration `20261031_01` retains Correction plans and extends closed submission
evidence. It permits zero effective principal while retaining attribution. It
also fixes deferred Transaction integrity validation when a coordinated update
changes the primary effect from Transfer to Expense: safe record access avoids
referencing a child-only field on a Transaction row. Financial validation is
otherwise preserved. Downgrade refuses every retained Correction submission
state or Correction record, and populated T11 upgrade/replay is covered.

## Delivery boundary

Backend implementation/review is only the producer candidate. After separately
authorized producer publication, the frontend must consume that committed P via
`pnpm api:update`, deliver reason/corrected-fact entry, complete preview,
confirmation/conflict recovery and visible history, and preserve generated
Fetch/Query/Zod ownership. Producer/consumer contract parity, exact-P/exact-C CI
and real P+C acceptance are still required before separately authorized issue
closure. No frontend or issue mutation is part of this backend task.
