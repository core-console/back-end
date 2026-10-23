# Finance v2 T06 accepted surplus producer

The [approved immutable specification](https://github.com/core-console/back-end/blob/e206bdd5af48a3f6b3b9b525c27fa6178220bdc7/docs/finance-v2-personal-receivable-settlement-spec.md)
owns product behavior. [Issue #32](https://github.com/core-console/back-end/issues/32)
bounds this backend producer. This adds explicit Income disposition to the
existing accumulated Settlement workflow. Selected subsets, forgiveness,
Corrections, credit application/return and conversion of existing credit into
Income are outside this slice.

## Explicit version 3 intent

Use the existing `previewFinanceSettlement` and `createFinanceSettlement`
operations. Version 1 remains ordinary principal repayment; version 2 remains
explicit `retainCredit`. Their canonical commands and preview tokens are
unchanged. Version 3 is specific to Settlement and requires:

- `excessDisposition: "acceptIncome"`;
- the existing Participant, receiving Account, receipt date, exact amount and
  optional note;
- `categoryAllocations` containing exactly one positive amount in the receipt
  currency, with an optional `categoryId` for an active Ledger-owned Category;
- at confirmation, `previewToken` and `Finance-Command-Version: 3`, plus the
  existing submission UUID and owner headers.

The classification amount must equal the actual unallocated receipt after all
eligible debt is paid in the existing oldest-Advance-date order. It cannot
classify principal as Income, leave unexplained surplus, consume existing credit,
or silently add a Category. Omitting Category explicitly represents uncategorized
Income under existing Finance rules. No split-category editor or defaults are
introduced. With no eligible debt, an explicit version 3 intent may accept the
whole actual receipt; it creates no invented Obligation.

Preview adds `acceptedSurplus`, its complete `categoryAllocations`, and unchanged
`availableCreditBefore`/`availableCreditAfter`. The existing receiving balance,
receipt, per-Obligation cash Allocations and remaining debt fields show the complete
result. A positive surplus and matching classification are required; a receipt
without surplus uses the existing principal repayment contract. The generated
[OpenAPI](../openapi/openapi.json) is authoritative for wire details.

## Financial result and protection

Receiving 102 against debt 100 produces principal Internal Transfers of 100 and
one linked Income of 2. The full cash effect is 102, debt becomes zero, and no
credit is created. Income has one ordinary Account Movement and one complete
Category Allocation. Receiving Liability Accounts use the existing inverse
account-relative sign. No new Transaction kind or balancing Account is added.

Settlement detail/history adds `acceptedSurplus` and `incomeTransactionId` and
includes the Income in `transactionIds`. Confirmed principal Allocation order is
retained. Existing retained credit origins and gross credit positions are
unchanged. Overview includes only accepted surplus in Income for its actual
financial date; recorded future dates affect Current Balance immediately.

The Income link protects replacement/deletion through ordinary Transaction
operations with `finance_linked_record_protected`. Existing receivable and credit
Account protections continue to apply. Unrelated manual bookkeeping remains
available and is never silently attributed to a Participant.

## Confirmation, recovery and migration

The token binds exact intent, receiving Account state, Participant, eligible
Obligations, credit origins and the selected Income Category. Confirmation takes
the existing Account UUID locks, then Participant, then Category when present.
Changed consequences or Category state require renewed preview and deliberate
confirmation. A refreshed classification is never silently substituted.

One durable submission commits Settlement, principal Allocations, Income,
Movements, Category Allocation, link and terminal evidence together. Keep the
same owner, Ledger, version, key and canonical body for lookup or explicit retry.
Missing lookup, infrastructure errors and failed projection refresh remain
unresolved. Bound commands still recover after new v3 admissions are closed.

Migration `20261023_01` adds the Income link and v3 command validator without
rewriting historical records or v1/v2 validators. Downgrade refuses any Income
link or unfinished/created/rejected v3 binding. Guarded PostgreSQL tests observe
commit independently before dropping responses, then recover through a restarted
application and verify that effects occur exactly once.

## Remaining consumer and acceptance work

After separate producer delivery approval, synchronize that committed producer P
using frontend `pnpm api:update`. The consumer must provide explicit surplus
acceptance and Category intent, show the complete financial preview/result, and
support v3 pending/conflict/recovery evidence through generated Fetch/Query/Zod.
It must preserve existing v1/v2 behavior and credit meaning.

Frontend delivery C, exact-P/exact-C CI, parsed contract correspondence, routed
production-preview Chromium/accessibility checks and real P+C financial
acceptance remain required. Backend validation does not complete those gates or
authorize Issue #32 closure. T07 selected scope and Settlement Corrections remain
separate later extensions.
