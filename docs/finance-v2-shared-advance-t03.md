# Finance v2 shared Advance producer (T03)

This extends the [T02 producer](finance-v2-pure-advance-t02.md) for the backend
portion of [Issue #29](https://github.com/core-console/back-end/issues/29).
Product authority remains the frozen
[Finance v2 specification at e206bdd5](https://github.com/core-console/back-end/blob/e206bdd5af48a3f6b3b9b525c27fa6178220bdc7/docs/finance-v2-personal-receivable-settlement-spec.md),
sections 4.2, 5.1 and 6; scoped acceptance is AC02–AC04.

## Versioned public contract

The same four Advance operations remain available. The backend-generated
`openapi/openapi.json` is authoritative. The T02 version `1` command grammar,
canonicalization, preview tokens, receipt evidence and source projection remain
supported, including unfinished original submissions. That grammar still
requires an Asset, zero owner share and exactly one participant.

For shared purchases or Liability funding, use `Finance-Command-Version: 2` on
`POST /api/finance/ledgers/{ledgerId}/advances`. Its body is:

```json
{
  "fundingAccountId": "UUID",
  "transactionDate": "2026-10-20",
  "amount": { "amount": "300.00", "currency": "CNY" },
  "ownerShare": { "amount": "100.00", "currency": "CNY" },
  "participantShares": [
    { "participantId": "Alex UUID", "amount": { "amount": "120.00", "currency": "CNY" } },
    { "participantId": "Blair UUID", "amount": { "amount": "80.00", "currency": "CNY" } }
  ],
  "note": "Shared purchase",
  "previewToken": "64 lowercase hexadecimal characters"
}
```

Preview uses the same body without `previewToken`, at `/advances/preview`.
The body shape selects the preview grammar. Unknown fields and mixed grammars
are rejected. The total must be positive, owner share non-negative, and each
participant share positive. At least one distinct external Participant is
required; duplicates are rejected rather than merged. Every amount uses the
funding Account currency and frozen exact Money precision. Shares must equal
the total exactly; there is no automatic share calculation or rounding.

Canonical version `2` participant shares are sorted by Participant UUID. Input
order is presentation only: reordering identical shares keeps the preview and
same-key recovery binding. Other edits cannot reuse an admitted action key.
Version `2` is supported only for `createFinanceAdvance`; the five Finance v1
commands keep version `1` validity and evidence meanings.

The shared preview includes the existing funding balance before/after,
`ownerExpense`, and `receivableIncrease`, plus `participantReceivables` with
each `participantId` and exact `principal`. It writes nothing. Confirmation
locks the funding Account, reads its balance after acquiring the lock, and
locks all Participants in stable UUID order. The token binds normalized intent,
Ledger, funding facts and Current Balance, and every Participant's facts.
Changed facts or eligibility require refreshed preview and explicit confirmation;
the original terminal rejection remains recoverable under its original key.

## Atomic financial composition

Each participant gets one Obligation and one exclusive receivable Asset with
zero Opening Balance, linked by an Internal Transfer from the funding Account.
A positive owner share creates one ordinary Expense with its complete
uncategorized Category Allocation. Zero owner share creates no Expense.
These use existing Finance Transaction kinds and Account Nature rules.

For 300 total, owner 100, Alex 120 and Blair 80:

| Effect | Asset funding | Liability funding |
| --- | --- | --- |
| Funding Account Movement total | -300 | +300 |
| Owner Expense | 100 | 100 |
| Alex receivable / principal | 120 | 120 |
| Blair receivable / principal | 80 | 80 |
| Income | 0 | 0 |

The shared source returns `transactionIds` for all linked financial effects and
`obligations` for all participants, alongside funding Account, calendar date,
confirmation timestamp, note, total and owner share. The original T02 source
continues to return its singular `transactionId` and `obligation`. Obligation
projections retain principal, cash-paid, forgiven, credit-applied and remaining;
resolution components start at zero. The [T04 cash repayment workflow](finance-v2-cash-repayments-t04.md)
derives cash-paid and remaining from retained Allocations. Totals are derived exactly from
owner share and Obligation principals. Existing financial position includes
receivable Accounts once; composition never adds a second balance authority.

All linked Expense and transfer Transactions reject ordinary replacement or
deletion. Every managed receivable rejects manual Account edits/archive, manual
Finance entries and Balance Adjustments. Funding remains available for unrelated
ordinary bookkeeping. Participant identity survives rename/archive; archived
Participants cannot receive new Advances. Ownership, currency, Account eligibility
and Tracking Start Date checks apply to the entire command.

## Recovery and migration

The existing submission UUIDv4, owner, operation, target Ledger and immutable
version/body must be durably prepared before dispatch. Admission precedes
execution. One execution transaction writes the complete Advance, Obligations,
Accounts, Transactions, Movements, Category Allocation and terminal outcome.
Retries serialize under the original submission identity. There are no separate
per-participant or per-Transaction submissions.

Lookup retains version `2` unfinished and terminal evidence with `no-store`.
Only correlated terminal evidence or guarded version-specific Q29 non-admission
resolves an action; generic failures, absent lookup or resource-read failures
do not authorize a new action identity. Closing a version to new admissions
does not remove its retained parser or prevent recovery of existing bindings.

Migration `20261018_02` preserves existing T02 sources and retained evidence,
adds the owner share and command version to the source, permits one Obligation
per distinct participant, and links every shared financial Transaction. The
database independently validates canonical version `2` submission structure,
Money, shares, identity order and exact total. Downgrade refuses any retained
version `2` source or submission, including unfinished and rejected actions.

Protected PostgreSQL tests cover public effects, reporting, both funding Natures,
exact Money, invalid/share/reference rejection, ownership, stale preview,
same-key concurrency, linked protection, populated migration, and interruption
and response-loss recovery. Independent committed-effect observations precede
retry. This producer does not add repayment, forgiveness, correction, credit
use, bill repayment, or cancellation workflows.

## Consumer handoff

After separate review and authorization, publish producer P through the harness
without issue linkage. Synchronize that committed producer with frontend
`pnpm api:update` and generated Fetch/Query/Zod. Frontend implementation,
consumer C delivery, matching P+C verification and issue closure remain separate.
No current candidate receipt establishes delivered cross-repository acceptance.
