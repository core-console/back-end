# Finance v2 T11 producer: return available credit

Implements the backend portion of canonical
[front-end Issue #46](https://github.com/core-console/front-end/issues/46),
under sections 5.7, 6 and 7.6, scoped AC22 and the return branch of AC46 of the
[immutable specification](https://github.com/core-console/back-end/blob/e206bdd5af48a3f6b3b9b525c27fa6178220bdc7/docs/finance-v2-personal-receivable-settlement-spec.md).
T05 is the ticket's direct blocker. T10 application remains independently usable;
corrections involving returned credit belong to T17, with cancellation in T20.

## Contract and financial effects

Use existing Ledger `/settlements/preview` and `/settlements` operations. Preview
accepts this closed draft; confirmation adds `previewToken` and requires
`Finance-Command-Version: 8` plus the existing submission owner/key headers:

```json
{
  "kind": "creditReturn",
  "participantId": "00000000-0000-0000-0000-000000000001",
  "payingAccountId": "00000000-0000-0000-0000-000000000002",
  "transactionDate": "2026-10-29",
  "amount": {"amount": "25.00", "currency": "CNY"}
}
```

Amount is positive exact Money. The paying Account must be active, unmanaged,
owned within this Ledger and in the action currency. An archived Participant
can resolve existing credit. Dates retain Finance v1 semantics: recorded future
effects apply immediately, and all affected Account tracking boundaries apply.
The optional note retains existing normalization. Debt selection, receiving
Account, forgiveness, surplus disposition and Category Allocation inputs are
not part of this action. Versions 1–7 keep their meanings.

Preview exposes paying balance before/after, total available credit before/after,
and ordered origins with receipt Settlement/Transaction, Liability Account,
origin date, immutable confirmation order, consumed amount and availability
before/after. Return 25 from origins 20 then 30 consumes 20 and 5, leaving 25 on
the newer origin, irrespective of editable financial dates. A return cannot
skip an older available origin to evade its Account tracking boundary.

Each origin creates a linked ordinary Internal Transfer. The paying Asset
decreases, or the paying Liability increases, by the consumed amount; the
origin's managed credit Liability decreases by that same amount. Normalized
effects cancel. No Expense, Income, Category Allocation, new Account, fictitious
receipt, debt Allocation or new Transaction kind is created. Tracked debt is
unchanged. Available credit is derived from receipt amounts less both confirmed
applications and actual returns; it cannot be consumed twice.

Settlement detail and Participant history add `CreditReturnResponse`, including
the paying Account, actual amount/date/note, immutable confirmation identity,
Transaction IDs and frozen ordered origin consumption. Later actions or
Participant rename/archive do not rewrite confirmed return history. Origin reads
retain original receipt amounts and show current availability; Participant and
Overview projections preserve gross receivable and credit positions.

## Atomicity, recovery and protection

Confirmation locks paying and affected credit Accounts in UUID order before the
Participant. It rereads authoritative state and compares an intent-bound preview.
Changed Account state/balance, Participant state, credit availability or entered
intent requires renewed preview and confirmation. A refreshed command uses a new
submission key; it cannot replace the original command retained under its key.

The existing durable submission transaction owns all return records, fragment
provenance, transfers, movements and terminal evidence. Failures roll back all
financial effects. Unknown outcomes remain unresolved; lookup or explicitly
retry the same owner/key/version/body after restart. Terminal replay returns
the original outcome without paying again. Guarded PostgreSQL tests observe
committed fragments, actual movements and terminal evidence through an independent
connection before dropping response delivery, then recover through a fresh app.

Migration `20261029_01` adds the explicit paying Account link, immutable
origin/transfer fragments and closed v8 command validation. Ledger-bound foreign
keys, unique fragment/Transaction links, positive amounts and non-negative
availability protect retained records. Populated T10 and older evidence survives
upgrade unchanged. Downgrade refuses any retained v8 state or return record.
Ordinary linked Transaction edits/deletes and managed credit Account edits,
manual entries or Balance Adjustments cannot bypass consistency. Unrelated
Finance v1 bookkeeping remains available.

## Remaining delivery and frontend dependencies

This uncommitted producer candidate requires independent Standards/Spec review
and separate user approval before commit or publication. After an approved
commit, regenerate committed-snapshot protected validation and review evidence.
Publish producer P through the backend harness **without issue linkage**; the
canonical issue is frontend-tracked.

From committed P, the frontend must synchronize OpenAPI and regenerate
Fetch/Query/Zod through `pnpm api:update`, implement accessible return entry,
paying Account/date/amount, oldest-origin and financial preview, deliberate
confirmation, history, and durable v8 preparation/lookup/retry. Validate generated
boundary semantics (including non-BMP input), keyboard/focus, applicable axe checks
and production-preview Chromium recovery. Deliver reviewed consumer C separately,
verify exact-P/exact-C CI and real scoped P+C acceptance, then obtain separate
closure authorization through the frontend tracker workflow.

Backend implementation and validation do not establish frontend acceptance or
issue completion. T12+ corrections, specification changes and frontend edits are
outside this producer candidate.
