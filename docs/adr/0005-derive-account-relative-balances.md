# Derive account-relative balances from an opening position

A Finance Account uses an account-relative signed balance: positive represents a
position in its declared Account Nature, while negative represents an
opposite-side position such as an overdrawn Asset or a credit on a Liability.
Its Current Balance is derived from its Opening Balance and subsequent Account
Movements. A cached or materialized balance may optimize reads but is not a
second authority.

## Considered alternatives

Net-worth signs would make Assets positive and Liabilities negative, simplifying
some cross-account arithmetic but making a Liability balance less natural to
read. Formal debit and credit semantics would import accounting machinery beyond
current requirements. An authoritative mutable Current Balance could answer
reads directly but could silently diverge from transaction history.

## Consequences

Cross-account interpretation normalizes balances and movements using Account
Nature: an Asset Account Movement keeps its account-relative sign and a Liability
Account Movement inverts it. Opening Balance records a pre-existing position as
of a Tracking Start Date; it is not Income, Expense, Internal Transfer, or
another Finance Transaction kind.
