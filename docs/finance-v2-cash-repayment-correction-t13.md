# Finance v2 cash repayment amount Correction (T13)

Backend producer for [Issue #37](https://github.com/core-console/back-end/issues/37),
bounded by the approved Finance v2 specification, sections 7.3 and 7.6;
scoped acceptance criteria AC31, AC40 and AC45. The specification is unchanged.
This note describes the producer contract, not delivered frontend or pair acceptance.

## Preview and confirmation

Within `/api/finance/ledgers/{ledgerId}`:

| Method and path | Operation |
| --- | --- |
| POST `/settlement-corrections/preview` | `previewFinanceSettlementCorrection` |
| POST `/settlement-corrections` | `createFinanceSettlementCorrection` |
| GET `/settlement-corrections/{correctionId}` | `getFinanceSettlementCorrection` |
| GET `/settlements/{settlementId}/corrections` | `listFinanceSettlementCorrections` |

The closed version-1 intent has these required fields:

```json
{
  "settlementId": "00000000-0000-4000-8000-000000000001",
  "amount": {"amount": "70.00", "currency": "CNY"},
  "reason": "Original receipt was entered as 80 instead of 70",
  "excessDisposition": "none",
  "categoryId": null
}
```

`amount` is the positive corrected original receipt, in the existing currency.
Money keeps the established decimal-string precision and durable range.
`reason` is trimmed and contains 1–500 Unicode code points.
Account, Participant, date, selection and cancellation are not command inputs.

Preview shows original confirmation identity/order, retained selection, before/after
receipt facts, original/effective cash Allocation amounts and immutable positions,
each affected Obligation's complete principal/cash/forgiven/credit/remainder
composition, all linked financial Transactions before/after, and exact Account
balances before/after. Confirmation adds the returned `previewToken` and the
existing `Idempotency-Key`, `Finance-Command-Version: 1` and
`Finance-Submission-Owner` headers. The generated OpenAPI is authoritative.

## Allocation and financial meaning

A decrease first removes unallocated receipt value, then releases effective cash
in reverse original confirmed Allocation order. Receipt 100 allocated 40 then 60
corrected to 70 leaves 40 and 30. Correcting 80 to 70 against principal 100 leaves
cash paid 70 and unpaid debt 30, with no automatic forgiveness.

An increase can resolve currently unpaid debt within the same originally confirmed
Allocation identities, in their retained order. It cannot select a newly introduced
Obligation, expand to unrelated debt, replay other Settlements, or consume credit.
This also permits restoration of a previously released Allocation, including one
whose effective cash is zero. Original amounts and positions remain retained.

Any excess requires `excessDisposition: retainCredit` or `acceptIncome` explicitly.
Use `none` exactly when no excess remains. `categoryId` is nullable for accepted
Income and must be null for other dispositions. A supplied Category must be active
and in the same Ledger. Retained credit and accepted surplus are shown separately;
no hidden netting, automatic Income, fabricated cash return or Expense is introduced.
Credit-only receipts can be corrected without inventing an Obligation.

The same Settlement identity, original confirmation time/order, receiving Account,
Participant, date, note and selection survive. Immutable Allocation `cash_paid`,
`position` and original `remaining_after` remain unchanged; nullable
`effective_cash_paid` overrides the current resolution projection. Existing
receivable queries and Settlement projections use the effective amount. Original
credit origin amount and confirmation provenance remain retained, with a nullable
effective amount, including zero, used by available-credit selection and consumers.

Cash and credit differences use linked Internal Transfers at the original receipt
date. These transfers restate the false original amount; they are protected
Correction effects, not new actual payments or credit-return Settlements. Asset and
Liability receiving Accounts retain their respective signs. Accepted surplus Income
is restated at that same original date, retaining the prior facts in Correction
history; zero surplus removes its false Income effect rather than creating Expense.
All four existing Finance Transaction kinds retain their meanings.

Original linked financial Transactions and new correction transfers remain protected
from independent manual replacement/deletion. Managed receivable and credit Accounts
remain protected from Account edits, manual transactions and Balance Adjustments.
Unrelated ordinary Finance v1 bookkeeping remains available.

## Recovery and boundaries

The new operation has its own closed version-1 command and `settlementCorrection`
resource receipt. Existing Advance Correction v1 and Settlement v1–v8 retain their
meanings. Admission commits the owner/key/operation/Ledger/canonical-intent binding;
one subsequent atomic commit contains effective amounts, financial restatements,
history and terminal outcome. Exact retry and `/api/finance/submissions/{submissionId}`
lookup recover that same outcome after restart or response loss. A changed intent
cannot reuse a bound key. Unknown errors and missing lookup are not non-commit proof.

Account locks precede Participant and source locks. Authoritative state is reread
after acquisition. Tokens bind the complete relevant original facts, effective
resolution, financial effects, balances, eligibility, intent and Correction revision.
A stale confirmation returns `finance_settlement_correction_preview_stale` and
requires a fresh complete preview and deliberate confirmation with a new submission.

This slice accepts standard cash-only receipts, including unconsumed retained credit
and explicitly accepted surplus. Forgiveness-containing Settlements, credit-application
and credit-return Settlements, and receipt origins with applied/returned credit return
`finance_settlement_correction_ineligible`. Their full coordinated lifecycle belongs
to later approved slices. Receiving-Account/date/attribution correction, Advance
after-settlement correction and cancellation are outside T13.

History retains actor, reason, immutable per-Settlement revision, confirmation time
and complete before/after consequences. Migration `20261102_01` preserves populated
T12 records and all older durable evidence. Downgrade refuses any retained T13
command state, history or effective override rather than erasing recovery evidence.

Backend verification covers public HTTP behavior, guarded real PostgreSQL
reconciliation and immutable order through linked Transaction replacement, exact
currency/large Money, ownership and bypass boundaries, rollback/commit-acknowledgement
faults, independently observed committed-response loss, lookup/retry and races.
Protected harness validation and independent Standards/Spec reports bind the final
candidate separately. Frontend delivery, exact P+C acceptance, publication and issue
closure remain subsequent separately authorized work.
