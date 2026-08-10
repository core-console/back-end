# Keep the backend OpenAPI contract authoritative

The Core Console backend owns the HTTP contract and produces its authoritative
OpenAPI representation. The backend commits the produced artifact so contract
changes can be reviewed, checked for drift, and synchronized; being checked in
does not make the artifact an authority independent of the backend contract that
produces it.

The ownership flow is:

```text
backend contract/schema
→ backend-owned OpenAPI artifact
→ synchronized consumer snapshot
→ generated consumer artifacts
```

Consuming repositories synchronize their own snapshot and generate their client,
query, and schema artifacts from it. Consumer snapshots and generated artifacts
do not redefine or own the backend contract. Producer and consumer drift checks
enforce this boundary.

## Considered alternatives

A schema-first shared contract package would move contract ownership into a
separately coordinated artifact, while a consumer-owned contract would let a
consumer define backend behavior. Those approaches can centralize
cross-repository versioning or consumer needs, but they separate ownership from
the backend behavior that implements the contract.

## Consequences

The chosen boundary accepts synchronized copies and an explicit synchronization
step in exchange for keeping contract ownership with the backend while allowing
each repository to validate independently.
