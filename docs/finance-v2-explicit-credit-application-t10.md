# Finance v2 T10 producer: explicit credit application

Implements the backend producer portion of [Issue #35](https://github.com/core-console/back-end/issues/35)
under sections 4.6, 5.7, 6 and 7.6, scoped AC21 and the application branch of AC46
of the [immutable specification](https://github.com/core-console/back-end/blob/e206bdd5af48a3f6b3b9b525c27fa6178220bdc7/docs/finance-v2-personal-receivable-settlement-spec.md).

## Contract

Use the existing Ledger `/settlements/preview` and `/settlements` operations.
Preview accepts this closed draft; confirmation adds its `previewToken` and
requires `Finance-Command-Version: 7` and the existing immutable submission
owner/key headers:

```json
{
  "kind": "creditApplication",
  "participantId": "00000000-0000-0000-0000-000000000001",
  "selectedObligationIds": ["00000000-0000-0000-0000-000000000002"],
  "transactionDate": "2026-10-27",
  "amount": {"amount": "25.00", "currency": "CNY"}
}
```

The amount is positive exact Money in one currency. Selection is a non-empty
unique set of eligible outstanding Obligations in the same Ledger, Participant
and currency, dated no later than the action. An optional note uses existing
Transaction normalization. Archived Participants can resolve existing debt and
credit. There is no receiving or paying Account input, forgiveness reason,
surplus disposition, automatic selection expansion or automatic credit use.
Versions 1–6 retain their existing meanings.

The preview returns `kind: creditApplication`, `amount`, selected outstanding
before/after, total available credit before/after, ordered `allocations` and
consumed `origins`. Credit origins are consumed by immutable receipt confirmation
order with stable identity tie-breakers, independently of financial dates.
Selected debt uses the existing oldest Advance date order and stable tie-breakers.
An origin can fund several debts and a debt can consume several origins. Each
typed Allocation fragment exposes position, Obligation and Advance identities,
source date, `originSettlementId`, `creditApplied` and `remainingAfter`.

Each consumed origin exposes its actual receipt Settlement and Transaction,
Liability Account, origin financial date, immutable confirmation order, consumed
amount and available before/after. Applying 25 from origins 20 then 30 consumes
20 and 5, leaving 25 on the second origin. Amounts exceeding selected debt or
available credit are rejected. The action cannot predate a consumed credit
Account's tracking start, preserving Finance v1 movement date integrity without
skipping an older origin. Future recorded actions affect Current Balance
immediately; target date eligibility still applies.

Confirmation returns the existing Settlement creation receipt with version 7.
Settlement detail and Participant history add `CreditApplicationResponse`, with
the explicit action kind, stable attribution, amount/date/note, selection,
immutable confirmation time/order, Transaction IDs, typed Allocations and frozen
origin-consumption amounts and balances. Later application, debt creation,
Participant rename or archive does not rewrite that confirmed history.
Obligation/Advance projections distinguish cash-paid, forgiven, credit-applied
and remaining amounts. Origin reads retain original receipt amounts and show
remaining availability; Participant positions keep gross debt and credit separate.

## Financial effects, persistence and recovery

Each origin-to-debt fragment creates one linked ordinary Internal Transfer.
Its source reduces the managed Receivable Asset and its destination reduces
the originating Credit Liability by the same exact amount. Their normalized
economic effects cancel. No cash Account changes, Income, Expense, Category
Allocation, new Account, fictitious receipt or new Transaction kind is created.
Applying credit 2 to debt 10 leaves debt 8, credit zero and cash/P&L unchanged.

Confirmation locks all affected Receivable and credit Accounts in UUID order
before locking the Participant, rereads authoritative composition/availability,
and compares the intent-bound preview. Changed selection, date, amount, note,
Participant state, selected debt or credit state requires a fresh preview and
deliberate confirmation. A refreshed preview is a new action with a new key;
it cannot replace the command retained under the original key.

Admission and execution use the existing durable submission protocol. All domain
records, origin-consumption fragments, transfers, movements and terminal evidence
commit or roll back together. Unknown failures remain unresolved. After response
loss or restart, look up or explicitly retry the same owner/key/version/body;
replaying a terminal command cannot consume credit again. Tests observe a real
PostgreSQL commit independently before dropping delivery, and inject failures
after fragment writes and around admission/terminal commits.

Migration `20261027_01` retains the closed v7 grammar, adds an explicit Settlement
action marker and origin-to-debt Allocation fragments with Ledger-bound foreign
keys, unique transaction/fragment links, positive exact amounts and non-negative
remainders. It preserves populated T09 and older evidence. Downgrade refuses
all retained v7 states and application records. Ordinary Account edits, manual
entries, Balance Adjustments and Transaction replacement/deletion cannot bypass
the managed positions or change linked application Transfers.

## Remaining frontend and delivery dependencies

This producer candidate is uncommitted and does not establish delivered P+C
acceptance. After separate producer approval, regenerate committed-snapshot
validation/review evidence and deliver P through the publication harness without
issue linkage. From that committed P, synchronize frontend OpenAPI and regenerate
Fetch, TanStack Query and Zod artifacts with `pnpm api:update`.

The consumer still needs explicit amount/target selection, credit-origin and
financial preview, pending confirmation protection, distinct credit-applied
composition/history and durable v7 command preparation, reopening, lookup and
retry. Keep original-command recovery separate from stale-preview renewal.
Validate generated boundary semantics, keyboard/focus, applicable axe checks and
production-preview Chromium behavior. Deliver reviewed C separately and verify
exact-P/exact-C CI plus the real scoped P+C workflow before separately authorized
Issue #35 closure. Backend CI alone cannot establish pair acceptance.

T11 credit return, credit-origin corrections, restoration and later correction
workflows remain outside this candidate. The approved specification is unchanged.
