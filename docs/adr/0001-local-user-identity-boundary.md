# Separate Local User identity from External Identity

Core Console assigns each Local User a stable UUID Local User ID. A complete
External Identity `(issuer, subject)` is the key used to resolve that Local User,
not the Core Console entity identifier itself, and each complete pair resolves
to at most one Local User. Username and email are profile attributes rather than
identity keys.

This intentional indirection keeps Local User identity independent from mutable
profile data and from any one identity provider's identifier scheme.

## Considered alternative

Using an External Identity or profile attribute directly as the Local User
identifier would remove the resolution step, but it would couple Core Console
identity to external identifier schemes or mutable descriptive data.

## Consequences

Current workflows that operate on a Local User use its Local User ID, but this
decision does not mandate how every future domain must reference users. Each
future domain defines its reference semantics according to what it refers to.

This decision does not establish policies for identity migration, relinking, or
associating multiple External Identities with one Local User.
