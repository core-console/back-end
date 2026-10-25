# Finance v2 T09 producer: combined repayment and forgiveness

Implements the backend portion of [Issue #34](https://github.com/core-console/back-end/issues/34)
under sections 5.5 and 6 and scoped AC15/AC16 of the
[immutable specification](https://github.com/core-console/back-end/blob/e206bdd5af48a3f6b3b9b525c27fa6178220bdc7/docs/finance-v2-personal-receivable-settlement-spec.md).

## Contract

Use the existing Ledger `/settlements/preview` and `/settlements` operations.
Preview accepts this closed draft. Confirmation adds its `previewToken` and
requires `Finance-Command-Version: 6` plus the existing immutable submission
owner/key headers:

```json
{
  "kind": "combined",
  "participantId": "00000000-0000-0000-0000-000000000001",
  "receivingAccountId": "00000000-0000-0000-0000-000000000002",
  "selectedObligationIds": ["00000000-0000-0000-0000-000000000003"],
  "transactionDate": "2026-10-25",
  "amount": {"amount": "98.00", "currency": "CNY"},
  "forgivenessAmount": {"amount": "2.00", "currency": "CNY"},
  "reason": "Explicitly waive the remaining two"
}
```

`amount` is actual cash, never cash plus forgiveness. Both Money values must be
strictly positive, exact and in the receiving Account's currency. Reason is
trimmed, non-empty and at most 500 characters; an optional `note` uses ordinary
Transaction note normalization. Selection is a non-empty unique set of eligible
outstanding Obligations for this Ledger, Participant, currency and event date.
Archived Participants may resolve existing debt. A managed receivable or credit
Liability cannot be the receiving Account. Versions 1–5 retain their meanings.

The preview has `kind: combined`, cash `amount`, `forgivenessAmount`, matching
`expense`, receiving balances, selected outstanding before/after and one ordered
`allocations` list. Cash entries have `kind: cash` and `cashPaid`; subsequent
non-cash entries have `kind: forgiveness` and `forgiven`. Every entry retains
position, Obligation/Advance identities, source date and its stage's
`remainingAfter`. Cash allocates oldest Advance date first with existing stable
tie-breakers, then forgiveness uses the remaining selected capacity in that same
order. Selection input order does not affect intent or allocation order.

For debts 40 then 60, cash 90 produces cash Allocations 40 and 50, followed by
forgiveness 10 on the second debt. Cash 98 plus forgiveness 2 resolves debt 100;
cash 98 plus forgiveness 1 leaves 1 outstanding. Excess cash or forgiveness
beyond the post-cash selected remainder is rejected. There is no automatic
shortfall clearing, selection expansion, Income, credit creation or credit use.

Confirmation returns the existing versioned Settlement creation receipt.
Settlement detail and Participant history add `CombinedSettlementResponse`,
retaining actual cash, forgiveness total, reason, selection, event date, stable
Participant UUID, confirmation time/order, typed ordered Allocations and linked
Transaction IDs. Obligation and Advance projections retain separate cash-paid,
forgiven and remaining composition. Later backdated debt and Participant rename
or archive do not redistribute the confirmed result.

## Financial execution and recovery

One Settlement records all cash and forgiveness together. Each cash Allocation
creates a linked ordinary Internal Transfer reducing its managed receivable and
increasing the receiving Asset (or decreasing the receiving Liability). Each
forgiveness Allocation creates a linked ordinary Expense, a negative primary
receivable movement and a complete uncategorized Category Allocation. No new
Transaction kind is introduced. Existing linked-record protection covers every
cash/Expense Transaction and managed receivable, including manual entries,
replacement/deletion, Account edits and Balance Adjustments.

Confirmation locks affected Accounts in UUID order before the Participant,
rereads authoritative balances/composition and checks the intent-bound preview.
Changed state or intent requires fresh preview and deliberate confirmation.
The domain rows, transfers, Expenses, movements, Category Allocations and terminal
submission evidence commit or roll back together. After an uncertain outcome,
look up or explicitly retry the original owner/key/version/body. A renewed preview
cannot replace a retained command under the same key; it is a new action.

Migration `20261025_01` adds the closed v6 retained grammar, permits a reason on a
cash Settlement, and allows distinct cash and forgiveness Allocations for one
Obligation within that Settlement. It preserves existing receipts, positions and
history. Downgrade refuses every retained v6 submission state and combined record.
Guarded PostgreSQL tests verify malformed evidence rejection, populated T08
upgrade, rollback after linked cash and Expense writes, admission/terminal commit
acknowledgement loss, committed-response loss observed independently, restart,
same-key replay, concurrency, exact currencies and linked consistency protections.

## Remaining delivery and frontend dependency

This uncommitted producer candidate does not deliver the frontend workflow.
After separate producer approval and committed-snapshot validation, deliver P
through the publication harness without issue linkage. From that committed P,
run frontend `pnpm api:update` to synchronize OpenAPI and regenerate Fetch,
TanStack Query and Zod artifacts. The consumer must implement one combined
preview/confirmation, distinct paid/forgiven history and original v6 command
recovery, with stale-preview renewal kept separate from original-command retry.

Deliver reviewed C separately, verify exact-P/exact-C CI and the real scoped
Chromium/TCP/PostgreSQL pair, and only then seek separately authorized Issue #34
closure. Neither this candidate nor backend validation establishes delivered
P+C acceptance. T10, credit application/return and Correction workflows remain
outside this implementation. The approved product specification is unchanged.
