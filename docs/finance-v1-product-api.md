# Finance v1 Product and API Specification

Status: review draft

## 1. Purpose

Finance v1 is the first usable personal-bookkeeping release for Core Console. It
supports fast manual recording, a calendar-first understanding of recent
activity, multiple economically isolated Finance Ledgers, and exact
currency-aware balances without becoming an enterprise accounting product.

This specification defines:

- Finance v1 user workflows and product behavior;
- the backend-owned Finance HTTP/OpenAPI capabilities;
- public request and response semantics;
- product-relevant validation and Problem Details behavior;
- behavioral requirements for the later frontend UI/UX phase; and
- explicit v1 non-goals and deferred questions.

It does not define a database schema, persistence model, frontend visual design,
or implementation structure.

## 2. Authority and terminology

The authoritative Finance vocabulary is defined in [`CONTEXT.md`](../CONTEXT.md).
This specification uses that vocabulary without duplicating the glossary.

The settled Finance foundation remains authoritative:

- [ADR-0003: Own each Finance Ledger by one Local User](adr/0003-own-finance-ledgers-by-local-user.md)
- [ADR-0004: Use an atomic transfer-aware ledger](adr/0004-use-an-atomic-transfer-aware-ledger.md)
- [ADR-0005: Derive account-relative balances from an opening position](adr/0005-derive-account-relative-balances.md)
- [ADR-0006: Denominate each Finance Account in one currency](adr/0006-denominate-each-finance-account-in-one-currency.md)

The backend owns the HTTP contract and generated OpenAPI artifact according to
[ADR-0002](adr/0002-backend-owned-openapi-contract.md). The eventual consumer
flow remains:

```text
backend contract/schema
-> backend-owned OpenAPI artifact
-> synchronized frontend snapshot
-> generated frontend client/query/schema artifacts
```

## 3. Product goals

Finance v1 must let an active Local User:

- explicitly create and switch among multiple Finance Ledgers;
- manage Asset and Liability Finance Accounts;
- manage optional Ledger-owned Categories;
- record Income and Expense quickly, including by keyboard;
- record same-currency Internal Transfers;
- correct a known actual balance through a target-balance Balance Adjustment;
- understand present financial position and selected-month activity without
  combining unlike currencies;
- browse a selected day and complete Transaction history;
- correct or delete mutable manual bookkeeping atomically; and
- use these workflows throughout the supported desktop width range.

## 4. Finance v1 feature scope

### 4.1 Supported capabilities

Finance v1 includes:

- explicit first-Ledger onboarding;
- Ledger create, list, switch, and rename;
- Account create, list, edit, archive, and unarchive;
- Category create, list, rename, archive, and unarchive;
- one complete Category Allocation per Income or Expense write;
- uncategorized Income and Expense;
- Income, Expense, same-currency Internal Transfer, and Balance Adjustment;
- optional plain-text notes on every Transaction kind;
- current Account balances and per-currency Ledger financial position;
- selected-month Income, Expense, and net by currency;
- sparse per-day activity for a month calendar;
- selected-day detail through Transaction history;
- paginated Transaction history with the v1 filters defined below;
- full same-kind replacement for ordinary Transactions;
- specialized target-balance replacement for Balance Adjustments; and
- atomic Transaction deletion.

### 4.2 Supported real-world use through primitives

The following scenarios require no dedicated product subsystem:

- Cash is an Asset Account. A bank withdrawal is an Internal Transfer to Cash,
  and a cash purchase is an Expense from Cash.
- A separately administered team fund uses its own Finance Ledger so it does
  not pollute personal balances or statistics.
- Lending principal can move to and from a receivable Asset through Internal
  Transfers. Interest, when applicable, is Income.
- A shared expense can be represented as an Expense for the owner's share and
  an Internal Transfer to a receivable Asset for the amount owed by others.
  Repayment is another Internal Transfer.
- A full refund may be represented by deleting the original Expense. A partial
  refund may be represented by replacing the original Expense with the intended
  net amount.

These representations do not add a dedicated loan, AA/shared-expense, or Refund
workflow.

## 5. User workflows

### 5.1 First use and Ledger selection

Opening Finance when no Ledger exists presents explicit first-Ledger creation.
The frontend may suggest the name `Personal`, but opening Finance must not create
a Ledger as an implicit backend side effect.

After successful creation, the frontend selects the new Ledger. The backend does
not persist a current or default Ledger. The frontend may remember the last
valid selection as navigation state:

- if the remembered Ledger still exists, restore it;
- otherwise select the first Ledger in the deterministic list order; and
- if no Ledger exists, return to first-Ledger onboarding.

Switching Ledger preserves the selected month and date. It clears Account and
Category selections and any entry draft scoped to the previous Ledger.

Ledger list order is case-insensitive name order with the opaque identifier as a
stable tie-breaker if required. This ordering has no Finance chronology meaning.

### 5.2 Empty Ledger and Account onboarding

A Ledger with no Accounts still has a readable Overview. Financial-position and
month-summary currency groups are empty because no Account establishes a
currency, and calendar `days` is empty.

Ordinary Transaction entry is unavailable when the Ledger has no active
Accounts and directs the user to Account creation. Account creation requires the
frontend to submit all of:

- name;
- Account Nature;
- currency;
- Opening Balance; and
- Tracking Start Date.

The frontend may prefill a zero Opening Balance in the selected currency and its
local current calendar date as the Tracking Start Date. They remain explicit
submitted values, not hidden backend defaults.

A Ledger with only archived Accounts remains browseable but cannot create a new
Transaction until an Account is created or unarchived.

Zero Categories never blocks Income or Expense. Its single v1 Category
Allocation may be uncategorized.

### 5.3 Finance Overview

The landing page is calendar-first and scoped to one selected Ledger and month.
It provides:

- present Account balances and Ledger financial position;
- selected-month Income, Expense, and net;
- a month calendar with daily activity;
- a selected date and that date's Transaction detail; and
- quick Income/Expense entry using the selected date.

The initial frontend presentation selects its local current calendar month and
date. The backend does not define a Local User, Ledger, or application timezone.

Month navigation preserves the selected day-of-month when it exists in the new
month and otherwise clamps it to the new month's final valid day. Selecting a
calendar day changes both selected-day detail and the quick-entry Transaction
Date.

When Accounts exist but no Transactions exist:

- calendar `days` is empty; and
- selected-month Income, Expense, and net are zero for the currencies
  represented by those Accounts.

### 5.4 Keyboard-first Income and Expense entry

The inline common path supports Income and Expense. Expense is the initial kind.
The selected calendar date supplies Transaction Date.

The user supplies:

- a strictly positive amount;
- an active Account;
- an optional Category; and
- an optional note.

The request Money currency comes from the selected Account. The frontend may
suggest or order Categories differently for Income and Expense, but Category is
not permanently kind-restricted.

Pressing Enter submits a valid form without requiring pointer interaction. A
validation or conflict failure preserves the draft and the user's current
context.

After a successful submission, the frontend:

- retains Ledger, selected date, Transaction kind, Account, and Category;
- clears amount and note;
- returns keyboard focus to amount entry; and
- refreshes affected Account balances, financial position, month summary,
  calendar activity, and selected-day detail.

Exact controls and focus styling belong to frontend UI/UX design.

### 5.5 Internal Transfer

Internal Transfer is a secondary workflow. The user supplies:

- an active source Account;
- a distinct active destination Account in the same Ledger;
- one strictly positive Money amount;
- Transaction Date; and
- an optional note.

Finance v1 requires both Accounts to use the same currency. The command amount
uses that currency. The Transaction remains one atomic event with two Account
Movements and does not use Category Allocations.

The product does not enforce an available-balance or insufficient-funds rule.
Negative Account Balances remain valid under the settled account-relative
balance semantics.

### 5.6 Balance Adjustment

Balance Adjustment is a secondary target-balance workflow:

1. Select an Account and Transaction Date.
2. Read the balance-adjustment context.
3. Display the derived comparison balance and Account Nature.
4. Enter the known actual account-relative target balance and optional note.
5. Optionally preview the correction delta.
6. Submit the target command with the exact expected derived balance and
   expected Account Nature from that context.

For a historical Transaction Date, target balance means the known actual
end-of-day Account Balance on that calendar date. The comparison basis includes:

- Opening Balance where applicable; and
- every Account Movement with Transaction Date less than or equal to the
  selected date.

It excludes Account Movements dated after that date. This does not establish
intra-day ordering among Transactions sharing a date.

Immediately before applying the command, the backend atomically recomputes the
comparison balance and reads the current Account Nature. If the balance differs
from `expectedDerivedBalance`, the backend writes nothing and returns
`409 account_balance_changed`. Otherwise, if Account Nature differs from
`expectedAccountNature`, the backend writes nothing and returns
`409 finance_account_semantics_changed`.

For either stale-context conflict, the frontend refreshes the context, shows the
new balance and Account Nature, and requires explicit resubmission. It must never
silently reuse the previous target against changed comparison semantics.

When the expected balance still matches:

```text
correction delta = target balance - derived comparison balance
```

Create behavior:

- a non-zero delta creates one Balance Adjustment and returns `created`;
- a zero delta creates no Finance Transaction and returns `noChange`.

Replacement behavior:

- the comparison basis treats the existing Adjustment as absent;
- a non-zero delta atomically removes the prior effect, applies the replacement,
  and returns `updated`;
- a zero delta atomically removes the existing Adjustment and returns `removed`.

Replacement may change Account, Transaction Date, target balance, and note. A
replacement that keeps an existing archived Account reference is permitted;
changing to another Account requires an active Account.

The durable Transaction stores and returns only the signed, non-zero
account-relative correction delta. Target balance is command input, never a
second persistent balance authority.

The frontend gives distinct feedback for `created`, `updated`, `removed`, and
`noChange`.

### 5.7 Transaction history, detail, edit, and delete

Selected-day detail reuses the Ledger-scoped history query with equal inclusive
`fromDate` and `toDate`. Complete history uses the same query and cursor
pagination.

Both selected-day detail and complete history provide a path to Transaction
detail, edit, and delete.

Finance Transaction kind is immutable. Income, Expense, and Internal Transfer
use full same-kind replacement. A Transaction recorded with the wrong kind is
deleted and recreated rather than morphed into another financial-event shape.

Every replacement atomically revalidates and replaces the complete financial
effect. It may retain an existing archived Account or Category reference in the
same role. A replacement reference must select an active resource; a Category
may instead become uncategorized.

Balance Adjustment replacement uses the specialized target-balance workflow,
not ordinary Transaction replacement.

Deletion requires explicit frontend confirmation because it immediately changes
derived balances and statistics. The backend atomically removes the Finance
Transaction, its Account Movements, and its Category Allocations. Finance v1 has
no undo or restore behavior.

### 5.8 Account management

Finance v1 supports Account create, list, edit, archive, and unarchive. Account
deletion is outside v1.

Account PATCH may edit:

- name;
- Opening Balance;
- Tracking Start Date;
- Account Nature when its correction preconditions hold; and
- currency when its correction preconditions hold.

Status changes only through archive and unarchive commands.

Name remains ordinarily editable. Nature or currency may change only when, at
the time of the correction:

- no Finance Transaction exists for the Account; and
- Opening Balance is zero.

An edit cannot satisfy that precondition merely by combining a non-zero Opening
Balance replacement with a Nature or currency change in one request. Once the
Account has a financial position or history, an attempted Nature or currency
change returns `409 finance_account_semantics_locked`.

When an eligible zero-position Account changes currency, its zero Opening
Balance is returned in the new currency. No historical Money is converted.

Opening Balance and Tracking Start Date remain correctable. Opening Balance must
retain the Account currency. When Transactions exist, a corrected Tracking Start
Date cannot be later than the earliest Transaction Date affecting that Account.
Current Balance is recomputed from the corrected Opening Balance and all Account
Movements.

Archiving is allowed even when historical Transactions reference the Account or
Current Balance is non-zero. An archived Account:

- remains part of historical data and financial-position calculations;
- remains visible where balances or history require it;
- is excluded from ordinary new-Transaction selectors;
- may be unarchived; and
- does not prevent an existing Transaction retaining its reference from being
  edited or deleted.

Account list order is active before archived, then case-insensitive name order,
then opaque identifier.

### 5.9 Category management

Finance v1 supports Category create, list, rename, archive, and unarchive.
Category deletion is outside v1. The backend does not seed default Categories.

Archiving is allowed when historical Category Allocations reference the
Category. An archived Category:

- remains readable in Transaction history;
- remains attached to existing Category Allocations;
- is excluded from ordinary new-entry suggestions; and
- may be unarchived when its name does not conflict.

An edit may preserve its existing archived Category reference. Replacing the
reference requires an active Category or an explicitly uncategorized Allocation.

Category list order is active before archived, then case-insensitive name order,
then opaque identifier.

## 6. Common API contract

### 6.1 Route and OpenAPI prefix

This specification shows runtime routes with the `/api` prefix. The generated
OpenAPI document continues to represent `/api` through its server URL and
therefore uses path keys beginning with `/finance`, not `/api/finance`.
Implementations must not produce an `/api/api/...` consumer route.

All Finance business endpoints use the existing active-Current-User boundary.
There is no Finance-specific RBAC or sharing policy in v1.

### 6.2 JSON conventions

- Public JSON fields, path parameters, and query parameters use lower camel
  case.
- Finance identifiers are UUIDs exposed as opaque consumer identifiers.
- Request objects reject unknown fields and accept their declared public JSON
  names only.
- Response objects are closed public projections rather than persistence
  records.
- Transaction kind values are `income`, `expense`, `internalTransfer`, and
  `balanceAdjustment`.
- Account Nature values are `asset` and `liability`.
- Account and Category status values are `active` and `archived`.

### 6.3 Money

Money uses this JSON shape:

```json
{
  "amount": "35.00",
  "currency": "CNY"
}
```

Rules:

- `amount` is a base-10 decimal string, never a JSON number.
- Scientific notation, thousands separators, binary floating-point semantics,
  and precision beyond the supported currency scale are rejected.
- Request values may use fewer fractional digits than the supported scale.
- Responses are canonicalized to the supported currency's `minorUnit` scale.
- `currency` must be present in the backend-owned supported-currency catalog.
- Economic Amounts, Category Allocation amounts, and v1 Transfer command amount
  are strictly positive.
- Opening Balance, Account Balance, target balance, and other account-relative
  position values may be signed or zero where their semantics allow it.
- A durable Balance Adjustment correction delta is always non-zero.
- Money tied to an Account must use that Account's currency.

This is an HTTP/product contract and does not select integer-minor-unit versus
fixed-decimal persistence.

### 6.4 Dates and month

- Transaction Date and Tracking Start Date use ISO `YYYY-MM-DD`.
- Overview month uses ISO `YYYY-MM`.
- Dates carry no timestamp or timezone semantics.
- Finance v1 permits future Transaction Dates.

A future-dated Transaction is an already-recorded financial event, not a
scheduling instruction. Its Account Movements participate in Current Balance
immediately. Finance v1 does not delay application until the date arrives.

### 6.5 Names and notes

Ledger, Account, and Category names:

- are trimmed;
- must remain nonblank;
- contain at most 100 Unicode characters; and
- are plain text.

Ledger names are case-insensitively unique per Local User. Category names are
case-insensitively unique within one Ledger, including archived Categories.
Account names need not be unique.

Ledger and Category uniqueness uses the same locale-independent backend Unicode
case-folding rule after trimming for create, rename, and unarchive. Finance v1
adds no separate locale-sensitive comparison or Unicode normalization policy.
Conflicts are never silently renamed.

Transaction `note`:

- is optional plain Unicode text;
- is trimmed;
- normalizes blank content to null;
- contains at most 500 characters; and
- has no Markdown, HTML, or other markup semantics.

Ordinary characters that resemble markup are valid plain text and must be
rendered safely without interpretation.

## 7. Required API surface

All paths in this table are runtime paths.

| Method | Runtime path | `operationId` | Success |
| --- | --- | --- | --- |
| `GET` | `/api/finance/currencies` | `listFinanceCurrencies` | `200` collection |
| `GET` | `/api/finance/ledgers` | `listFinanceLedgers` | `200` collection |
| `POST` | `/api/finance/ledgers` | `createFinanceLedger` | `201` Ledger |
| `PATCH` | `/api/finance/ledgers/{ledgerId}` | `updateFinanceLedger` | `200` Ledger |
| `GET` | `/api/finance/ledgers/{ledgerId}/accounts` | `listFinanceAccounts` | `200` collection |
| `POST` | `/api/finance/ledgers/{ledgerId}/accounts` | `createFinanceAccount` | `201` Account |
| `PATCH` | `/api/finance/ledgers/{ledgerId}/accounts/{accountId}` | `updateFinanceAccount` | `200` Account |
| `POST` | `/api/finance/ledgers/{ledgerId}/accounts/{accountId}/archive` | `archiveFinanceAccount` | `200` Account |
| `POST` | `/api/finance/ledgers/{ledgerId}/accounts/{accountId}/unarchive` | `unarchiveFinanceAccount` | `200` Account |
| `GET` | `/api/finance/ledgers/{ledgerId}/categories` | `listFinanceCategories` | `200` collection |
| `POST` | `/api/finance/ledgers/{ledgerId}/categories` | `createFinanceCategory` | `201` Category |
| `PATCH` | `/api/finance/ledgers/{ledgerId}/categories/{categoryId}` | `updateFinanceCategory` | `200` Category |
| `POST` | `/api/finance/ledgers/{ledgerId}/categories/{categoryId}/archive` | `archiveFinanceCategory` | `200` Category |
| `POST` | `/api/finance/ledgers/{ledgerId}/categories/{categoryId}/unarchive` | `unarchiveFinanceCategory` | `200` Category |
| `GET` | `/api/finance/ledgers/{ledgerId}/overview` | `getFinanceOverview` | `200` Overview |
| `GET` | `/api/finance/ledgers/{ledgerId}/transactions` | `listFinanceTransactions` | `200` page |
| `POST` | `/api/finance/ledgers/{ledgerId}/transactions` | `createFinanceTransaction` | `201` Transaction |
| `GET` | `/api/finance/ledgers/{ledgerId}/transactions/{transactionId}` | `getFinanceTransaction` | `200` Transaction |
| `PUT` | `/api/finance/ledgers/{ledgerId}/transactions/{transactionId}` | `replaceFinanceTransaction` | `200` Transaction |
| `DELETE` | `/api/finance/ledgers/{ledgerId}/transactions/{transactionId}` | `deleteFinanceTransaction` | `204`, no body |
| `GET` | `/api/finance/ledgers/{ledgerId}/accounts/{accountId}/balance-adjustment-context` | `getBalanceAdjustmentContext` | `200` context |
| `POST` | `/api/finance/ledgers/{ledgerId}/balance-adjustments` | `createBalanceAdjustment` | `200` command result |
| `PUT` | `/api/finance/ledgers/{ledgerId}/balance-adjustments/{transactionId}` | `replaceBalanceAdjustment` | `200` command result |

Ledger, Account, Category, and currency collections are unpaginated in v1.
Transaction history alone is paginated. Archive and unarchive commands are
idempotent and return the resulting resource representation.

## 8. Resource requests and responses

### 8.1 Currency catalog

`listFinanceCurrencies` returns a bare deterministic collection sorted by
currency code ascending. Each entry contains:

```json
{
  "code": "CNY",
  "minorUnit": 2
}
```

The normative initial Finance v1 supported-currency catalog is exactly:

| Code | `minorUnit` |
| --- | ---: |
| `CNY` | `2` |
| `USD` | `2` |
| `JPY` | `0` |

No other currency code is supported in the initial v1 contract. The API response
sorts these entries by code ascending as `CNY`, `JPY`, `USD`.

Finance v1 supports the codes present in this backend-owned catalog. Each entry
uses an ISO 4217 currency code and a backend-supported standard minor-unit scale.
The catalog is the single authority for Account currency validation, Money
precision validation, and frontend currency selection. Future catalog expansion
is additive product evolution and does not change the Finance Money wire shape.

The catalog is deterministic backend data and requires no external service. It
does not contain currency symbols, localized names, FX rates, conversion data,
or reporting-currency policy. The frontend may display the code itself.

### 8.2 Ledger

Ledger response:

```json
{
  "id": "<uuid>",
  "name": "Personal"
}
```

Create requires `name`. PATCH is name-only and uses omitted-field semantics.
There is no Ledger archive, delete, current, or default operation in v1.

### 8.3 Account

Account response:

```json
{
  "id": "<uuid>",
  "name": "Alipay",
  "nature": "asset",
  "currency": "CNY",
  "openingBalance": { "amount": "100.00", "currency": "CNY" },
  "trackingStartDate": "2026-08-01",
  "currentBalance": { "amount": "65.00", "currency": "CNY" },
  "status": "active"
}
```

Create requires every field except derived `currentBalance`, assigned `id`, and
managed `status`. PATCH uses omitted-field semantics for the editable fields in
section 5.8; it cannot change `status`.

Account list returns active and archived Accounts in the deterministic order
defined in section 5.8.

### 8.4 Category

Category response:

```json
{
  "id": "<uuid>",
  "name": "Food",
  "status": "active"
}
```

Create requires `name`. PATCH is name-only. Category list returns active and
archived Categories in the deterministic order defined in section 5.9.

### 8.5 Embedded references

Transaction responses embed current lightweight references:

```json
{
  "id": "<uuid>",
  "name": "Alipay",
  "status": "active"
}
```

Account and Category references contain opaque identifier, current name, and
current archival status. A nullable Category reference represents an
uncategorized Allocation. These are current projections, not historical label
snapshots; renaming changes how historical Transactions are returned.

## 9. Finance Transaction contract

### 9.1 Common response fields

Finance Transaction responses are a discriminated union. Every variant contains:

- `id`;
- `ledgerId`;
- `kind`;
- `transactionDate`; and
- nullable `note`.

They do not expose creation/update timestamps, persistence versions, database
keys, audit fields, or storage details.

### 9.2 Income and Expense

Income and Expense response variants add:

- `account`: an Account reference;
- `economicAmount`: strictly positive Money; and
- `categoryAllocations`: a collection of Category Allocations.

Each Category Allocation contains:

- strictly positive Money `amount`; and
- nullable Category reference `category`.

Finance v1 responses retain the collection shape, but v1 writes require exactly
one Allocation whose amount equals the complete Economic Amount. The Allocation
currency equals the Account and Economic Amount currency.

Create and replacement requests contain:

- `kind`: `income` or `expense`;
- `accountId`;
- `transactionDate`;
- positive `economicAmount`;
- optional nullable `note`; and
- exactly one `categoryAllocations` item containing positive `amount` and
  nullable `categoryId`.

Creation requires an active Account. Replacement may preserve its existing
archived Account or Category reference in the same role; a changed reference
must be active, or the Allocation must explicitly become uncategorized.

The backend derives the one Account Movement according to Transaction kind and
Account Nature. Callers do not submit a raw movement sign.

### 9.3 Internal Transfer

Internal Transfer response adds:

- `sourceAccount`;
- positive `sourceAmount`;
- `destinationAccount`; and
- positive `destinationAmount`.

Finance v1 requires those Money values to have equal currency and magnitude.
They remain distinct response fields so the projection does not assert that a
future cross-currency Transfer must have one nominal amount.

Create and replacement requests contain:

- `kind`: `internalTransfer`;
- `sourceAccountId`;
- `destinationAccountId`;
- one positive `amount`;
- `transactionDate`; and
- optional nullable `note`.

The Accounts must be distinct, active for new references, in the addressed
Ledger, and denominated in the Money currency. Internal Transfer has no Economic
Amount and no Category Allocations.

### 9.4 Balance Adjustment

Balance Adjustment response adds:

- `account`; and
- signed, non-zero account-relative `correctionDelta`.

It has no Economic Amount, Category Allocations, or persistent target balance.

#### Context read

`getBalanceAdjustmentContext` requires query parameter `transactionDate` and
optionally accepts `replacingTransactionId` when preparing replacement.

It returns:

- `account`;
- `transactionDate`;
- exact Money `derivedComparisonBalance`; and
- `accountNature`, representing the Account Nature used for the comparison.

When `replacingTransactionId` is supplied, the identified in-scope Balance
Adjustment is treated as absent from the comparison. An out-of-scope identifier
returns `finance_transaction_not_found`; an in-scope non-Adjustment identifier is
invalid request input.

Preparing a create command requires an active Account. Preparing a replacement
may retain the existing archived Account; selecting a different archived Account
returns `finance_account_archived`.

#### Create and replacement commands

Both commands submit:

- `accountId`;
- `transactionDate`;
- `expectedDerivedBalance`;
- `expectedAccountNature`;
- `targetBalance`; and
- optional nullable `note`.

Create does not accept a Transaction identifier. Replacement identifies the
existing Adjustment in the path and may change the Account.

Immediately before writing, the backend atomically verifies the recomputed
derived comparison balance and current Account Nature. A balance mismatch writes
nothing and returns `409 account_balance_changed`. Otherwise, an Account Nature
mismatch writes nothing and returns `409 finance_account_semantics_changed`.
Currency changes are already detected by exact Money comparison because
`expectedDerivedBalance` includes currency; no separate currency precondition,
revision counter, row version, lock token, or audit mechanism is added.

The command result is specific to this workflow:

```json
{
  "outcome": "created",
  "transaction": {}
}
```

`outcome` is `created`, `updated`, `removed`, or `noChange`. `transaction` is a
full Balance Adjustment response for `created` and `updated`, and null for
`removed` and `noChange`.

### 9.5 Ordinary replacement and deletion

`replaceFinanceTransaction` accepts only the complete Income, Expense, or
Internal Transfer request variants. The submitted kind must equal the stored
kind. It cannot create or replace Balance Adjustment.

If the in-scope target kind differs from the submitted kind, the response is
`409 finance_transaction_kind_immutable`. The same code applies when the
specialized Balance Adjustment replacement targets an in-scope non-Adjustment
Transaction. Scoped lookup happens first, so an out-of-scope identifier remains
`404 finance_transaction_not_found` without revealing kind.

`deleteFinanceTransaction` applies to all four kinds and returns `204` with no
body.

## 10. Transaction history query

`listFinanceTransactions` is scoped to one Ledger and supports:

- optional inclusive `fromDate`;
- optional inclusive `toDate`;
- optional `accountId`;
- optional `kind`;
- optional `categoryId`;
- optional `uncategorized=true`;
- optional opaque `cursor`; and
- optional `pageSize`, default 50 and allowed range 1 through 100.

Rules:

- all supplied active filters combine conjunctively, so every returned
  Transaction must satisfy every supplied filter;
- when both dates are supplied, `fromDate` must be less than or equal to
  `toDate`;
- `accountId` matches a Transaction involving that Account, including either
  side of an Internal Transfer;
- `categoryId` matches Income or Expense allocated to that Category;
- `uncategorized=true` matches Income or Expense whose v1 Allocation has no
  Category; it does not classify Internal Transfer or Balance Adjustment as
  uncategorized;
- `uncategorized=false` is rejected with `422 validation_error`; the only valid
  supplied value is `true`, and clients omit the parameter when the filter is
  inactive;
- `categoryId` and an active uncategorized filter are mutually exclusive;
- a cursor is opaque and tied to the original filter and ordering context; and
- invalid cursors or page sizes return `validation_error`.

Ordering is fixed:

1. Transaction Date descending;
2. one stable server-defined tie-breaker for deterministic pagination and
   presentation.

The tie-breaker is not exposed as Finance same-day economic chronology. Same-day
domain ordering remains deliberately unsettled.

Response:

```json
{
  "items": [],
  "nextCursor": null
}
```

`nextCursor` is null when no next page exists. V1 has no total-count guarantee,
note/full-text search, configurable ordering, or generic query-builder behavior.

## 11. Finance Overview contract

`getFinanceOverview` requires query parameter `month` in `YYYY-MM` format and
returns:

- `ledger`;
- requested `month`;
- every Account with present Current Balance, including archived Accounts;
- `financialPositionByCurrency`;
- `monthSummaryByCurrency`; and
- sparse calendar `days` containing only dates with Finance activity.

### 11.1 Present financial position

Each currency group contains:

- `currency`;
- signed Money `assetTotal`;
- signed Money `liabilityTotal`; and
- signed Money `netPosition`.

The calculations retain account-relative signed balances:

```text
asset total = sum of Account Balances for Asset Accounts
liability total = sum of Account Balances for Liability Accounts
net position = asset total - liability total
```

Opposite-side Account Balances are not clamped or reinterpreted. Archived
Accounts remain included because archiving does not erase their financial
position.

Account balances and financial position are present values at request time, not
historical month-end values. They include every already-recorded Transaction,
including one with a future Transaction Date.

### 11.2 Selected-month summary

Each currency group contains:

- `currency`;
- non-negative Money `income`;
- non-negative Money `expense`; and
- signed Money `net`, where `net = income - expense`.

The requested month controls these statistics. Internal Transfer and Balance
Adjustment never contribute to Income, Expense, or net.

Every currency represented by a Ledger Account receives a group, including a
zero-valued group when that month has no Income or Expense.

### 11.3 Calendar activity

`days` is sparse and includes only dates in the requested month that contain at
least one Finance Transaction. Each day contains:

- `date`;
- total `transactionCount`;
- `transactionCountByKind` for all four kinds; and
- `activityByCurrency`.

Each currency activity group contains:

- `currency`;
- non-negative Money `income`;
- non-negative Money `expense`;
- signed Money `net`; and
- `transactionCount` for Transactions involving that currency.

Counts are Finance Transaction counts, not Account Movement counts. One
Internal Transfer counts once even though it contains two movements. A
Transfer-only or Adjustment-only date therefore remains visible without its
amount being classified as Income or Expense.

Different currency groups are never aggregated. Selected-day Transaction detail
is intentionally not duplicated in Overview and comes from the history query.

## 12. Ownership and lookup behavior

Every Finance lookup is scoped through the active Current User, addressed
Ledger, and addressed nested resource.

- A missing or non-owned Ledger returns `404 finance_ledger_not_found`.
- An Account outside the addressed Ledger returns
  `404 finance_account_not_found`.
- A Category outside the addressed Ledger returns
  `404 finance_category_not_found`.
- A Transaction outside the addressed Ledger returns
  `404 finance_transaction_not_found`.

This behavior also applies when the identifier exists in another Ledger owned by
the same Local User. The API does not disclose out-of-scope existence.

Malformed UUIDs return `422 validation_error`. The existing active-user boundary
returns `403 access_denied`.

## 13. Validation and Problem Details

Every declared error response uses `application/problem+json`, stable safe
product language, and no raw database or internal exception detail.

### 13.1 Shared codes

| Status | Code | Meaning |
| --- | --- | --- |
| `403` | `access_denied` | The active-user access boundary rejects the request. |
| `422` | `validation_error` | The request or requested Finance state violates ordinary contract validation. |
| `503` | `database_not_configured` | PostgreSQL is not configured for the application. |
| `503` | `database_unavailable` | PostgreSQL is unavailable. |
| `500` | `internal_error` | An unexpected internal failure occurred. |

### 13.2 Finance not-found codes

| Status | Code |
| --- | --- |
| `404` | `finance_ledger_not_found` |
| `404` | `finance_account_not_found` |
| `404` | `finance_category_not_found` |
| `404` | `finance_transaction_not_found` |

### 13.3 Finance conflict codes

| Status | Code | Meaning |
| --- | --- | --- |
| `409` | `finance_ledger_name_conflict` | The Local User already owns a case-insensitively equal Ledger name. |
| `409` | `finance_category_name_conflict` | The Ledger already contains a case-insensitively equal Category name, including archived Categories. |
| `409` | `finance_account_semantics_locked` | Nature or currency cannot change because Opening Balance is non-zero or Transaction history exists. |
| `409` | `finance_account_archived` | A new Transaction reference attempts to use an archived Account. |
| `409` | `finance_category_archived` | A new Allocation reference attempts to use an archived Category. |
| `409` | `finance_transaction_kind_immutable` | An in-scope replacement attempts the wrong write workflow or a different kind. |
| `409` | `account_balance_changed` | Balance Adjustment comparison no longer equals `expectedDerivedBalance`. |
| `409` | `finance_account_semantics_changed` | The Account Nature used by the Balance Adjustment context changed after the context was read, so the target requires review against a fresh context. |

Create, rename, and Category unarchive use their corresponding name-conflict
code. Archive/unarchive actions are otherwise idempotent.

`validation_error` covers ordinary validation without creating additional stable
codes, including:

- unsupported catalog currency;
- malformed Money, JSON-number Money, scientific notation, or excessive
  precision;
- Money whose currency does not match its Account;
- blank or over-length names and notes;
- invalid or reversed date ranges;
- a Transaction Date before an affected Account's Tracking Start Date;
- invalid cursors or page sizes;
- simultaneous Category and uncategorized filters or `uncategorized=false`;
- missing, multiple, non-positive, incomplete, or incorrectly summed v1
  Category Allocations;
- identical Internal Transfer Accounts;
- mismatched or cross-currency Transfer inputs;
- unsupported Transaction request variants; and
- invalid Balance Adjustment context or command input.

## 14. Responsive and frontend behavioral requirements

Finance v1 remains desktop-first and supports the existing Core Console product
scope at viewport widths greater than or equal to 1024px. It does not create a
separate mobile product.

Across supported widths:

- Ledger and month navigation remain reachable;
- currency-aware summaries remain understandable;
- calendar day selection remains usable;
- quick entry remains usable with coherent keyboard order;
- selected-day detail remains reachable;
- complete Transaction history remains operable; and
- the page does not require page-level horizontal scrolling.

The month Calendar retains a functional seven-day structure. As horizontal
space decreases:

- cells may reduce inline monetary detail while retaining date, activity, and
  selection affordances;
- selected-day detail may reflow below the Calendar;
- currency summary groups may wrap or stack; and
- quick-entry fields may wrap or stack while preserving semantic and keyboard
  order.

Later UI/UX and implementation validation at 1024px must use the real Core
Console application shell. It must not assume Finance receives an isolated
1024px-wide content canvas.

Exact breakpoints, grid dimensions, panel placement, field arrangement,
typography, truncation, component dimensions, colors, and visual styling remain
frontend UI/UX decisions.

## 15. Explicit v1 non-goals

Finance v1 does not include:

- Ledger archive/delete or backend-owned current/default Ledger;
- Account or Category deletion;
- backend-seeded default Categories or a settled frontend suggestion set;
- multiple Category Allocation create/edit workflows;
- dedicated AA/shared-expense orchestration;
- same-day economic ordering;
- note/full-text search, configurable Transaction sorting, or query-builder APIs;
- cross-currency Transfer workflows;
- FX rates, conversion, reporting/base currency, or cross-currency totals;
- historical Account or Category label snapshots;
- explicit Refund kind/workflow, reversal, void, audit history, or
  reconciliation;
- recurring Transactions, scheduled Transactions, delayed application, or
  scheduled jobs;
- bank synchronization or statement import;
- budgets or budget limits;
- reports center, net-worth trends, or advanced analytics;
- investments, securities, or portfolio tracking;
- installments or a dedicated loan-management product;
- Category hierarchy, tags, or automation;
- tax functionality;
- durable merchant or Counterparty models;
- receipt attachments, OCR, or AI categorization;
- notifications;
- household/shared ownership or multi-owner Ledger semantics;
- Finance RBAC or Keycloak authorization design;
- mobile-specific product behavior; or
- frontend visual design or implementation.

## 16. Deliberately deferred product questions

The following questions are intentionally deferred until real usage creates a
requirement:

- whether and how Ledgers may later be archived or deleted;
- whether unused Accounts or Categories may later be deleted;
- the exact optional frontend Category suggestion set;
- full split-Transaction creation and editing UX;
- whether AA/shared-expense usage justifies a composite convenience workflow;
- whether Transaction notes require search;
- whether same-day economic ordering is needed;
- how cross-currency Transfers, FX, and valuation would work;
- whether historical label snapshots or audit history become necessary;
- whether explicit Refund, reconciliation, import, or automation workflows are
  justified; and
- whether Core Console's supported viewport scope later expands below 1024px.

These are product questions, not gaps that implementation may fill implicitly.
No new ADR is required for the reversible v1 choices in this specification.
