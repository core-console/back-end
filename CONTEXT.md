# Core Console Identity

This glossary defines the authoritative vocabulary for identity concepts owned
by the Core Console backend.

## Language

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
