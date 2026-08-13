# Use an atomic transfer-aware ledger

A Finance Transaction represents one atomic financial event and contains the
Account Movements needed to represent it. An Internal Transfer moves value
between two Finance Accounts in the same Finance Ledger as one transaction, so
its financial effects cannot be mutated independently. Creating, changing, or
removing a Finance Transaction applies all its Account Movements atomically.
This preserves deterministic account balances without requiring full
double-entry accounting.

Each Finance Transaction has an explicit kind: Income, Expense, Internal
Transfer, or Balance Adjustment. The kind is constrained by its Account
Movements and the Asset or Liability nature of the affected Finance Accounts;
it is not inferred solely from movement signs and is not an unchecked label.

The Finance Ledger models only Finance Accounts treated as owned within its
boundary. Income and Expense cross that boundary without creating external
balancing Finance Accounts for payers, payees, merchants, or other outside
parties. Optional external-party information may be added later as descriptive
product data without making it part of the balancing ledger model.

For an Asset Account Movement, its Normalized Economic Effect equals its
account-relative change; for a Liability Account Movement, the effect is the
inverse. Income requires one strictly positive effect, Expense requires one
strictly negative effect, and their Economic Amount is the exact Money magnitude
of that effect. Their Account Movement cannot be zero.

Account Movements use account-relative signs. Internal Transfer correctness
therefore accounts for Account Nature instead of requiring raw movement signs
to sum to zero. A future cross-currency Internal Transfer may also contain
different nominal amounts in its two account currencies while remaining one
atomic Finance Transaction.

Finance v1 constrains Income and Expense to exactly one Account Movement and an
Internal Transfer to exactly two Account Movements affecting distinct Finance
Accounts in the same Finance Ledger. For a same-currency Internal Transfer,
one normalized effect is negative, the other is positive, and their non-zero
magnitudes are equal. A future cross-currency Internal Transfer has the same
effect directions but may have different exact magnitudes, so Internal Transfer
has no single currency-neutral Economic Amount. These cardinalities are v1
product scope rather than permanent limits on future compound financial events.

A Balance Adjustment represents a known discrepancy whose economic cause is
unknown or intentionally not reconstructed. It has a Transaction Date and
exactly one non-zero Account Movement, in the Finance Account's currency, whose
account-relative sign may be positive or negative. The movement is the durable
correction delta and participates in Current Balance derivation; the Balance
Adjustment does not mutate a separate authoritative balance, create an external
balancing account, have an Economic Amount, or contribute to Income or Expense.
A zero delta creates no Finance Transaction.

A Finance v1 workflow may accept a known actual account-relative balance and
atomically calculate the correction delta against the Current Balance. That
target is command input, not a second balance authority or a persistent target
that re-derives the adjustment when earlier history changes. This workflow does
not establish reconciliation semantics.

## Considered alternatives

Representing a transfer as linked independent transactions is simpler locally
but permits its sides to diverge. Full double-entry accounting would provide a
complete balancing model, but Core Console has no current requirement for a
chart of accounts, accounting equity, formal debit and credit semantics, or
complete accounting statements.

Inferring economic kind solely from movement direction would avoid storing an
explicit classification, but the same Expense can decrease an Asset or increase
a Liability. Category also cannot determine economic kind because it describes
purpose rather than the financial event's balance semantics.

Representing outside parties as balancing Finance Accounts would make every
Income and Expense structurally balanced, but would expand the owned-account
model toward a broader double-entry chart without a current requirement.

## Consequences

Every Income and Expense has one or more strictly positive Category Allocations
in its movement currency whose sum equals its Economic Amount. An allocation may
omit Category to represent uncategorized value; Internal Transfer and Balance
Adjustment have no Category Allocations.

Finance v1 uses mutable manual bookkeeping: a Finance Transaction may be edited
or deleted, and every affected Account Movement and Category Allocation changes
atomically so Current Balances remain derived correctly. If the cause of a
Balance Adjustment is later reconstructed, the real transaction may be added or
corrected and the adjustment edited or deleted. Finance v1 does not require a
separate Refund kind or append-only void/reversal history. Those history
semantics may be introduced later if a concrete audit or reconciliation
requirement appears. The decision does not prescribe a database schema.
