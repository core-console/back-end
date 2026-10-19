# Finance v2 T04 cash repayment producer

The [approved specification](finance-v2-personal-receivable-settlement-spec.md)
at `e206bdd5af48a3f6b3b9b525c27fa6178220bdc7` is the product authority;
[issue #30](https://github.com/core-console/back-end/issues/30) bounds this slice.
This command records one Participant's cash receipt against accumulated eligible
debt. Selected subsets, forgiveness, surplus dispositions, credit operations and
Corrections belong to later commands.

## HTTP and command boundary

All paths below are relative to `/api/finance/ledgers/{ledgerId}`:

| Method and path | Operation ID | Purpose |
| --- | --- | --- |
| `POST /settlements/preview` | `previewFinanceSettlement` | Read current consequences without recording an action |
| `POST /settlements` | `createFinanceSettlement` | Confirm one grouped action under the submission protocol |
| `GET /settlements/{settlementId}` | `getFinanceSettlement` | Inspect retained receipt facts, transfers and ordered Allocations |
| `GET /participants/{participantId}/settlements` | `listFinanceParticipantSettlements` | List cash activity by event date, then immutable confirmation order |

The version `1` draft contains `participantId`, `receivingAccountId`,
`transactionDate`, exact positive `amount` Money, and optional `note`.
Confirmation additionally requires the matching `previewToken` and the existing
`Idempotency-Key`, `Finance-Command-Version: 1`, and `Finance-Submission-Owner`
headers. The generated [OpenAPI](../openapi/openapi.json) owns exact schemas.
Unknown fields are rejected, including subset, forgiveness and credit inputs.

The Participant must belong to the addressed Ledger, which must belong to the
active owner. Archived Participants may repay existing debt. The receiving
Account must be active, unmanaged, in the same Ledger and currency, with a
Tracking Start Date on or before the receipt date. Both Account Natures use
Finance v1 Internal Transfer meanings: receipt increases an Asset or reduces a
Liability. A separate card bill transfer does not create participant Allocations.
Already recorded future-dated receipts affect Current Balance immediately.

## Allocation, financial effects and history

Eligible Obligations have the same Participant, Ledger and currency and an
Advance date on or before the receipt date. Order is Advance date, immutable
Advance confirmation timestamp, then Obligation UUID. Names and presentation
order never determine allocation. Cash applies oldest first, preserving exact
partial remainders; 40 then 60 with a receipt of 50 becomes 40 plus 10, and
receiving 98 against 100 leaves 2. Cash exceeding eligible debt, including no
eligible debt, is rejected without a surplus or credit interpretation.

Preview shows receiving balance before/after, eligible outstanding debt
before/after, and each proposed cash Allocation and remainder. Confirmation
retains each Allocation's explicit position, amount and remainder at that time,
plus an immutable Settlement confirmation order. Later backdated Advances do
not replay or redistribute history. Settlement detail's `remainingAfter` is the
retained confirmation result; current debt is available through the existing
Participant Obligation and Advance projections.

Each Allocation creates one existing Internal Transfer: the dedicated
receivable Asset decreases and the receiving Account has the matching economic
increase. The complete Settlement, Allocations, Transactions, Movements and
terminal submission outcome commit together. There is no principal Income,
Expense, Category Allocation, new Transaction kind or separate editable balance.
`cashPaid` is derived from retained Allocations; `remaining` is principal less
cash paid. Forgiveness and credit-applied remain explicit zero Money in T04.

Every linked repayment Transaction rejects ordinary replacement/deletion with
409 `finance_linked_record_protected`. Existing managed-receivable protections
continue to reject manual entries, Balance Adjustments and Account edits/archive.
Unrelated receiving/funding Account bookkeeping remains available. No Settlement
edit/delete or direct participant reassignment endpoint is introduced.

## Concurrency, stale confirmation and recovery

Confirmation locks addressed Accounts in ascending UUID order, then the
Participant, and reads authoritative balances after lock acquisition. All debt
mutations for that Participant serialize with Advance creation and other
repayments. Relevant Account/Participant state and eligible Obligation
composition are bound into the token. A changed token returns terminal 409
`finance_settlement_preview_stale`, even if another receipt has exhausted the
old capacity. Refresh consequences and deliberately reconfirm as a new action
only after correlating that terminal rejection.

Admission commits before execution. Retries retain the original owner, Ledger,
operation, version, UUID and canonical body; same-key replay returns the original
receipt without executing effects again. Lookup via `getFinanceSubmission` is
read-only and `no-store`, and distinguishes unfinished from terminal evidence.
Closing admission does not remove the retained parser or prevent bound recovery.
Lock timeout is nonterminal `finance_submission_busy`; infrastructure failures,
generic errors, absent lookup and failed resource reads do not establish
non-admission. Only correlated terminal evidence or guarded command-validation
non-admission proof resolves the action.

Migration `20261019_01` preserves existing source/receipt data, adds the
Settlement and ordered Allocation tables, and extends canonical command and
outcome constraints without changing existing versions. Its SQL validator
independently checks closed command structure, identifiers, dates and exact
positive Money. Downgrade refuses retained Settlement sources or any unfinished,
created or rejected repayment binding.

## Consumer handoff

This is backend producer work. After separately authorized producer delivery,
synchronize that committed revision through frontend `pnpm api:update`, then
implement and review the generated-client consumer and verify matching P+C.
Backend tests and local receipts do not establish delivered UI or pair acceptance.
Issue #30 remains open until those separately authorized delivery/closure gates
are satisfied through the repository harness.
