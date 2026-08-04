# Identity Context

## Glossary

- **External Identity** — A complete `(issuer, subject)` pair from an identity source.
- **Local User** — A user provisioned in this application and identified internally by a stable `users.id`.
- **Current User** — The Local User projection resolved from an External Identity for the current request; it may be active or disabled.
- **Provisioned** — A Local User record exists before identity resolution runs.
- **Active** — A Local User state allowed through the active-user access boundary.
- **Disabled** — A Local User state that can be resolved as Current User but cannot pass the active-user access boundary.

## Identity mapping

- An External Identity is identified by the complete `(issuer, subject)` pair.
- Each complete External Identity maps to at most one Local User.
- External Identity is used to resolve a Local User, not as a long-term foreign key for business data.
- Future business data should reference the stable internal `users.id`; no business foreign-key instance exists yet.

## Provisioning and access

- Users must be provisioned before identity resolution.
- Identity resolution never automatically creates, activates, or seeds a user.
- Only an active Local User can pass the active-user access boundary.
- Missing and disabled users both return `403 access_denied` to clients, without revealing whether the user exists or is disabled.

## Scope boundary

- The development identity adapter constructs External Identity from server-controlled Settings.
- Future validated OIDC or other adapters should produce the same External Identity boundary.
- RBAC, automatic provisioning, and production OIDC are not implemented.
- See the existing [README.md](README.md#deferred-capabilities) notes for deferred capabilities; this document does not redesign them.
