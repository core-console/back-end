# Finance v2 T08 producer: standalone forgiveness

Implements the backend portion of [Issue #33](https://github.com/core-console/back-end/issues/33)
under sections 5.5 and 6 and scoped AC17/AC25 of the
[immutable specification](https://github.com/core-console/back-end/blob/e206bdd5af48a3f6b3b9b525c27fa6178220bdc7/docs/finance-v2-personal-receivable-settlement-spec.md).

## Contract

Use the existing Ledger `/settlements/preview` and `/settlements` operations.
Preview accepts this closed draft; confirmation adds its `previewToken` and uses
`Finance-Command-Version: 5` with the existing submission owner/key headers:

```json
{
  "kind": "forgiveness",
  "participantId": "00000000-0000-0000-0000-000000000001",
  "selectedObligationIds": ["00000000-0000-0000-0000-000000000002"],
  "transactionDate": "2026-10-22",
  "amount": {"amount": "20.00", "currency": "CNY"},
  "reason": "Explicitly waive this debt"
}
```

An optional `note` retains the ordinary Transaction note normalization. Reason is
required, trimmed, non-empty and at most 500 characters. Money is exact and
strictly positive. The selected set must contain unique outstanding Obligations
for this Ledger, Participant, currency and event date. Archived Participants can
resolve existing debt. Allocation follows existing oldest-Advance ordering within
the selection; nothing spills into unselected debt.

Preview returns `expense`, selected `outstandingBefore`/`outstandingAfter`, and
ordered typed Allocations with `kind: forgiveness`, `forgiven`, and
`remainingAfter`. There is no receiving Account or receiving balance. Confirmation
returns the ordinary versioned Settlement creation receipt. Settlement detail and
Participant history expose a distinct forgiveness projection including retained
selection, reason, date, confirmation order, Expense Transaction IDs and Allocations.
Versions 1–4 keep their previous command and response meanings.

## Financial execution and consistency

Each forgiveness Allocation creates one linked ordinary Expense, one negative
primary movement on its managed Asset receivable, and one complete uncategorized
Category Allocation. The reason appears on the Expense. No cash movement, receipt,
Income, credit origin, new Account or Obligation is created. A Settlement with a
forgiveness reason has no receiving Account; its amount denotes forgiven principal.

For debts 40 and 60, forgiving 20 only from the second yields debt 40 + 40,
Expense 20, cash unchanged, cash-paid zero, and forgiven 20 on the second.
Obligation and Advance projections independently expose cash-paid, forgiven and
remaining composition. Cash repayment subsequently uses the reduced capacity.
Available credits remain separate and are never consumed by forgiveness; zero
net exposure does not mark unselected outstanding debt as paid.

Confirmation locks selected receivable Accounts in UUID order before the
Participant, then recomputes capacity and the intent-bound preview. Changed selected
debt or intent requires a fresh preview and confirmation. Domain rows, Expenses,
movements, Category Allocations and terminal evidence commit as one financial group.
Uncertain outcomes use the original durable submission identity for lookup/retry.
Linked Transaction replacement/deletion, receivable edits, manual Transactions and
Balance Adjustments remain protected through the existing managed-record boundary.

Migration `20261024_02` preserves legacy receipts and cash Allocations, extends the
closed versioned command grammar and adds typed cash/forgiveness constraints. Its
downgrade refuses retained unfinished, rejected or confirmed forgiveness evidence.
Guarded PostgreSQL tests cover malformed retained evidence, populated upgrade,
partial group rollback, lost commit acknowledgements, response loss, restart lookup,
same-key replay and no duplicate Expense. Public API tests check exact balances,
Expense, absence of fabricated cash, composition, stale previews, currency precision,
concurrent cash/forgiveness, ownership and manual-path protections.

## Remaining delivery

This producer candidate does not deliver the frontend entry, preview, confirmation
or recovery UI. After separate producer approval and committed-snapshot validation,
deliver producer P through the backend publication harness without `--issue`.
Generate the consumer from that committed P using `pnpm api:update`, deliver C,
and verify the scoped real Chromium/TCP/PostgreSQL workflow against exact P+C.
Issue closure requires separate authorization and the harness's resumed publication
at P. Combined payment and forgiveness (T09), Corrections and later tickets remain
outside this implementation.
