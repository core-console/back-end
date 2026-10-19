# Finance v2 T02 backend producer contract

Product authority is the [frozen specification](finance-v2-personal-receivable-settlement-spec.md)
at `e206bdd5af48a3f6b3b9b525c27fa6178220bdc7`, bounded by
[Issue #28](https://github.com/core-console/back-end/issues/28). This producer
supports one active Participant, one active Asset funding Account, one currency,
and zero owner share. Settlement and Correction remain later slices.

## Representation and reconciliation

Confirmation atomically creates one Advance, one Obligation, one dedicated
receivable Asset Account, and one existing Internal Transfer. The receivable
starts at zero with the Advance date as its Tracking Start Date. Funding decreases
by the principal and the receivable increases by exactly the same amount; no
Income, Expense, or Category Allocation is created. Future dates affect Current
Balance immediately. Negative funding balances retain Finance v1 semantics.

The dedicated Account's UUID and the linked Transaction are exposed in the
Advance/Obligation projections. Its technical name is descriptive, never the
identity or attribution key. No pre-existing manual receivable is adopted or
silently attributed. Existing Ledger financial position includes the receivable
once; debt composition explains that position rather than adding it again.

Ordinary manual creates, replacement, and Adjustment paths cannot use this
receivable Account. Ordinary Account edits/archive and linked Transaction
replacement/deletion return 409 `finance_linked_record_protected`. Creates retain
that business rejection in their submission receipt. These protections cover
Opening Balance changes as well as Movements. The original funding Account
remains available for unrelated bookkeeping and ordinary Account edits.
There is no Advance or Obligation edit/delete endpoint. Cancellation belongs to
T20A/T20B, with explicit coordinated history; this slice does not implement it.

## Public operations

All paths are relative to `/api/finance/ledgers/{ledgerId}` and require the
existing active Current User and addressed Ledger ownership boundaries.

| Method | Path | Operation ID | Success |
| --- | --- | --- | --- |
| POST | `/advances/preview` | `previewFinanceAdvance` | 200 preview |
| POST | `/advances` | `createFinanceAdvance` | 201 submission receipt |
| GET | `/advances/{advanceId}` | `getFinanceAdvance` | 200 confirmed source and Obligation |
| GET | `/participants/{participantId}/obligations` | `listFinanceParticipantObligations` | 200 Obligation array |

A preview draft contains `fundingAccountId`, `participantId`, `transactionDate`,
`amount`, `ownerShare`, `participantShare`, and optional `note`. All three amounts
are explicit exact Money objects. Funding is positive, owner share is zero, and
participant share equals funding exactly in the funding Account's currency.
Unknown fields, inconsistent shares, numeric JSON amounts, excess precision, and
non-civil dates are invalid. Funding may not precede its Tracking Start Date.

Preview has no persistence or financial effects and may be discarded. It returns
`previewToken`, `fundingBalance`, `fundingBalanceAfter`, `ownerExpense`, and
`receivableIncrease`. The confirmation body is the same draft plus `previewToken`.
The token binds the normalized draft, Ledger, funding Account facts and Current
Balance, and Participant facts. Confirmation locks the funding Account, reads its
balance in a fresh statement after locking, and locks the Participant. Changed
facts return terminal 409 `finance_advance_preview_stale`; changed eligibility
returns its explicit reference rejection. No automatic re-preview or altered
confirmation occurs. Refresh the preview, show the current consequences, and
require deliberate renewed confirmation under a new action identity after the
original terminal rejection has been correlated.

Confirmed source includes its UUID, funding Account, Transaction, date, note,
confirmation timestamp, amount, zero owner share, and Obligation. An Obligation
includes stable UUID, Ledger, Participant, Advance, source date, receivable Account,
principal, cash-paid, forgiven, credit-applied, and remaining Money. Newly created
Obligations have zero resolution components and remaining equals principal.
The [T04 cash repayment workflow](finance-v2-cash-repayments-t04.md) updates the
derived cash-paid and remaining projections without changing original principal.
The Participant list orders by source date, immutable confirmation timestamp, and
Obligation UUID. Rename/archive preserve attribution and inspection. Archived
Participants cannot receive new Advances; terminal replay still returns the
original outcome after rename/archive.

## Durable confirmation and recovery

`createFinanceAdvance`, command version `1`, is an additive sixth command in the
existing [submission protocol](finance-create-submission-protocol.md). The five
existing command grammars and evidence meanings remain valid. Manual Transaction
and Adjustment rejection unions additionally describe linked-record protection.
The backend-generated `openapi/openapi.json` owns the exact wire schema.

Before dispatch, the consumer must durably prepare the original submission UUIDv4,
owner, operation, target Ledger, version, and complete immutable confirmation
body. Preparation failure prevents dispatch. Required headers remain
`Idempotency-Key`, `Finance-Command-Version`, and `Finance-Submission-Owner`.
Preview does not replace durable client preparation.

The shared PostgreSQL `(localUserId, submissionId)` namespace prevents reuse across
operations or Ledgers. Admission commits before execution. A submission-row lock
serializes retries, followed by funding Account and Participant locks. All domain
and financial effects plus the terminal outcome commit together. An unknown
failure leaves unfinished admission and rolls back tentative work. Same-command
retry returns an immutable terminal receipt or executes the original unfinished
action; edited content cannot reuse its identity.

Created receipts identify `{ "type": "advance", "id": "UUID" }`. Existing
`GET /api/finance/submissions/{submissionId}` now includes Advance unfinished and
terminal variants and retains `Cache-Control: no-store`. Correlated business
rejection is terminal; guarded `commandValidationRejection` proves syntactic
non-admission only under the existing Q29 rules. Generic errors, missing lookup,
and resource refresh failure prove neither non-commit nor permission to record a
second action. A confirmed receipt remains resolved if a subsequent resource read
fails. Read endpoints never execute an action.

## Consumer integration and verification

Synchronize a separately approved, committed and delivered producer P through
the frontend's `pnpm api:update` workflow. Consume generated Fetch/Query/Zod and
extend the existing journal/recovery boundaries for this operation. Do not use
five unrelated Finance v1 creates to represent one Advance.

The producer tests exercise the guarded dedicated PostgreSQL target through HTTP:
exact effects and reporting, source/composition, preview discard, date/currency/
ownership/share validation, stale confirmation, rename/archive, linked protection,
same-key concurrency, partial writes, admission/commit acknowledgement loss, and
committed HTTP response loss. Recovery tests observe PostgreSQL effects through
an independent session before retry.

Backend evidence establishes this producer only. Routed consumer UI, durable
browser preparation, accessible preview/recovery, generated contract
correspondence, producer P/consumer C delivery, and pair verification are required
separately before Issue #28 closure. Producer publication must omit issue linkage
while that work remains, following the issue and repository publication harness.
