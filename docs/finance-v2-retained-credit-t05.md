# Finance v2 T05 retained credit producer

The [immutable approved specification](https://github.com/core-console/back-end/blob/e206bdd5af48a3f6b3b9b525c27fa6178220bdc7/docs/finance-v2-personal-receivable-settlement-spec.md)
owns product behavior; [Issue #31](https://github.com/core-console/back-end/issues/31)
bounds this producer slice. This extends the [T04 Settlement](finance-v2-cash-repayments-t04.md)
with explicit receipt-backed credit. Income disposition, selected Obligations,
forgiveness, credit application/return and Corrections remain later work.

## Versioned Settlement contract

Use the existing preview and confirmation paths. A version 2 draft adds required
`excessDisposition: "retainCredit"` to the T04 draft. Confirmation adds the
matching `previewToken` and uses `Finance-Command-Version: 2`, together with the
existing submission UUID and owner headers. The draft shape selects the preview
grammar; the header selects the confirmation grammar. Unknown fields, unsupported
dispositions and mixed versions are rejected. The generated
[OpenAPI](../openapi/openapi.json) owns the complete wire schemas.

Cash still applies to all eligible debt in the retained T04 order. Any remainder
is explicitly retained as credit, including when no eligible debt exists. Preview
adds `retainedCredit`, `availableCreditBefore` and `availableCreditAfter` in the
receipt currency. The receiving balance change covers the full actual receipt;
outstanding debt cannot become negative. A version 2 receipt with no remainder
creates no credit origin and uses the existing Settlement detail projection.

Each positive retained amount creates one origin linked to its actual Settlement,
one dedicated zero-opening Liability Account and one existing Internal Transfer.
The Liability increases by the retained amount; the receiving Asset increases or
receiving Liability decreases equally. The origin is not an Obligation. Principal
and credit effects together explain the complete receipt, with no Income,
Expense, Category Allocation or new Transaction kind. Financial position includes
the credit through its Liability Account exactly once.

Settlement detail includes `creditOrigin` when credit was created and includes
its transfer in `transactionIds`. Origin fields retain the source Settlement,
Participant, receiving and Liability Accounts, financial date, confirmed timestamp,
immutable confirmation order, amount and available amount. Available equals the
origin amount in this slice; no application or return workflow is introduced.

Paths relative to `/api/finance/ledgers/{ledgerId}`:

| GET path | Operation ID | Meaning |
| --- | --- | --- |
| `/participants/{participantId}/credits` | `listFinanceParticipantCredits` | Origins in immutable receipt confirmation order, then source UUID |
| `/participants/{participantId}/positions` | `getFinanceParticipantPositions` | Gross outstanding receivables, available credits and net exposure separately by currency |

Settlement history remains chronological by financial date, then confirmation
order. Origin ordering uses confirmation order even for backdated receipts.
Creating another Advance leaves credit untouched. Zero net exposure never hides
gross debt and credit. Archived Participants retain access to their history and
positions and may record receipts. Every read and write enforces Ledger ownership
and Participant/Account attribution.

## Consistency, recovery and compatibility

The original version 1 grammar, canonical body, preview token and recovery remain
supported. It still rejects overpayments and unallocated receipts. Version 2 is
operation-specific; ordinary Finance v1 commands retain version 1. The existing
version 2 Advance grammar is unchanged.

All Settlement, Allocation, credit origin, Account, Transaction, Movement and
terminal evidence writes commit together under the existing durable submission
protocol. Retry and lookup use the original owner, Ledger, version, key and body.
Infrastructure errors, missing lookup and failed resource refresh do not prove
non-admission. Recovery never implicitly records another receipt. Closing new
admissions retains bound recovery support.

Confirmation uses the existing Account UUID lock order followed by the Participant
lock. Version 2 tokens also bind credit origins; changed consequences require
refreshed preview and a deliberate new confirmation after correlated rejection.
Manual entries, Adjustments, Account financial edits/archive and linked
Transaction replacement/deletion cannot bypass managed-credit reconciliation;
they return `finance_linked_record_protected`. Unrelated Finance bookkeeping
continues to work.

Migration `20261022_01` adds the origin table and closed version 2 command
validator without rewriting historical versions or receipts. Downgrade refuses
retained credit origins or any unfinished, created or rejected version 2
Settlement binding. Protected tests cover real committed response loss, rollback,
idempotency, stale confirmation, ownership and populated migration preservation.

## Consumer dependency

After separately authorized producer delivery, synchronize the committed producer
through frontend `pnpm api:update`. Frontend retain-credit/no-debt flows, generated
consumer artifacts and exact producer/consumer browser acceptance remain required
before separately authorized Issue #31 closure. Backend candidate validation alone
does not establish delivered UI or pair acceptance.
