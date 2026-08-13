# Core Console Domain

This glossary defines the authoritative vocabulary for domain concepts owned by
the Core Console backend.

## Identity

**Local User**:
Core Console's own user entity. It is identified within Core Console by a Local
User ID.

**Local User ID**:
The stable UUID assigned by Core Console to identify a Local User.

**External Identity**:
A complete `(issuer, subject)` pair that identifies an identity within an
external identity source and is used to resolve a Local User. Each complete pair
resolves to at most one Local User.

**Profile Attributes**:
Descriptive or contact data such as username, display name, and email. Profile
Attributes are not Core Console identity keys.

**Identity Resolution**:
The association of an External Identity with a Local User for one request. It
identifies the Local User without deciding whether that Local User may proceed.

**Current User**:
The Local User associated with the External Identity for one request. Current
User is a request-scoped role of the Local User, not a separate entity or a
statement of access eligibility.

**Local User Lifecycle**:
The Active or Disabled state owned by Core Console. Lifecycle changes do not
provision, disable, or otherwise mutate an external identity provider.

**Active**:
The Local User lifecycle state eligible to pass the active-user access boundary.

**Disabled**:
The Local User lifecycle state that may still be identity-resolved but is
rejected by the active-user access boundary.

**Inactive**:
The management API value `inactive` represents Disabled; it is not the canonical
Core Console lifecycle state.
_Avoid_: Inactive when referring to the Core Console domain state.

## Finance

**Finance Ledger**:
The ownership boundary for Finance data. Each Finance Ledger is owned by exactly
one Local User.

**Finance Account**:
A financial position treated as owned within one Finance Ledger. Its Account
Nature determines how changes to its balance are interpreted, and it is
denominated in exactly one currency.

**Account Nature**:
The economic character of a Finance Account. Finance v1 recognizes Asset and
Liability.

**Asset**:
An Account Nature for a financial position such as cash, checking, or savings.

**Liability**:
An Account Nature for a financial obligation such as a credit card.

**Finance Transaction**:
One atomic ledger event explicitly classified as Income, Expense, Internal
Transfer, or Balance Adjustment, assigned a Transaction Date, and containing the
Account Movements needed to represent that event.

**Account Movement**:
An account-relative change to the financial position of one Finance Account
within a Finance Transaction. Its Money uses the Finance Account's currency.

**Normalized Economic Effect**:
The effect of an Account Movement after Account Nature is applied: an Asset uses
the account-relative change, while a Liability uses its inverse.

**Economic Amount**:
The exact Money magnitude of an Income's positive or an Expense's negative
Normalized Economic Effect. Internal Transfer and Balance Adjustment do not have
an Economic Amount.

**Account Balance**:
The account-relative Money position of a Finance Account. A positive balance is
in the account's declared Account Nature; a negative balance is an opposite-side
position.

**Opening Balance**:
The Account Balance that already exists when Core Console begins tracking a
Finance Account as of its Tracking Start Date. It is not a Finance Transaction.

**Current Balance**:
The Account Balance derived from a Finance Account's Opening Balance and all its
subsequent Account Movements.

**Money**:
An exact amount paired with its currency. Money never has binary floating-point
semantics.

**Transaction Date**:
The calendar date on which a Finance Transaction belongs in the Finance Ledger.

**Tracking Start Date**:
The calendar date establishing the ledger boundary for a Finance Account's
Opening Balance and subsequent Account Movements.

**Income**:
A Finance Transaction kind representing value received from outside the Finance
Ledger's owned Finance Accounts without modeling an external balancing account.

**Expense**:
A Finance Transaction kind representing value spent outside the Finance Ledger's
owned Finance Accounts without modeling an external balancing account.

**Internal Transfer**:
A Finance Transaction kind representing one atomic movement of value between two
Finance Accounts in the same Finance Ledger.

**Balance Adjustment**:
A Finance Transaction kind representing an exact, non-zero correction to one
Finance Account when its known real-world position differs from its derived
Account Balance and the economic cause is unknown or intentionally not
reconstructed.

**Category**:
A stable, Finance-Ledger-owned classification of the economic purpose of value
received or spent. It may classify Income or Expense Category Allocations but
does not classify Account Movements or Internal Transfers.

**Category Allocation**:
A strictly positive exact portion of an Income or Expense's Economic Amount that
may reference one Category. Every Income and Expense is completely allocated in
its movement currency, with no implicit uncategorized remainder.

**Split Transaction**:
An Income or Expense represented by more than one Category Allocation.
