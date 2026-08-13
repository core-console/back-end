# Own each Finance Ledger by one Local User

Finance data requires an explicit personal ownership boundary rather than being
shared across the Core Console instance by default. Each Finance Ledger is
therefore owned by exactly one Local User. Finance v1 allows one Local User to
own multiple Finance Ledgers so economically separate finances, such as personal
money and an administered team fund, do not share accounts, transactions, or
balances.

This boundary does not require a dedicated persisted Ledger entity and does not
decide ledger creation, naming, lifecycle behavior, defaults, or future access
and delegation policies. Any such policy would not change the invariant that
each Finance Ledger has exactly one Local User owner.

## Considered alternatives

An instance-owned ledger would make Finance data shared by default. A separate
Finance owner concept would add a new ownership abstraction before any distinct
owner or sharing requirement exists. Restricting a Local User to one Finance
Ledger would mix economically separate finances merely because one Local User
administers both.
