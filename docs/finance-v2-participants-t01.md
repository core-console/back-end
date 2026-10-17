# Finance v2 T01 backend producer contract

Product authority is the [approved specification](finance-v2-personal-receivable-settlement-spec.md),
sections 4.1 and 7.1, with AC05, AC06, and AC39 applied to the Participant-only
scope of [Issue #27](https://github.com/core-console/back-end/issues/27).
This note describes the producer integration choices; it does not extend the
product specification.

## Public API

All paths below are relative to `/api/finance/ledgers/{ledgerId}` and require the
existing active Current User boundary and ownership of the addressed Ledger.

| Method | Path | Operation ID | Success |
| --- | --- | --- | --- |
| POST | `/participants` | `createFinanceParticipant` | 201 Participant |
| GET | `/participants` | `listFinanceParticipants` | 200 Participant array |
| GET | `/participants/{participantId}` | `getFinanceParticipant` | 200 Participant |
| PATCH | `/participants/{participantId}` | `renameFinanceParticipant` | 200 Participant |
| POST | `/participants/{participantId}/archive` | `archiveFinanceParticipant` | 200 Participant |

Create and rename accept only `{ "name": "display name" }`. Names are trimmed,
must contain 1–100 characters after trimming, and may repeat. A name is descriptive
text, never an identity key or a link to a Local User. Every create assigns an
independent UUID within the addressed Ledger. The closed response is
`{ "id": "UUID", "ledgerId": "UUID", "name": "display name", "status": "active" }`,
with `status` equal to `active` or `archived`.

List includes both statuses, active first, then ordered within status by
case-folded name and UUID. Detail and rename remain available after archive.
Archive is idempotent and preserves UUID, Ledger, and financial state. There is
no delete or unarchive operation. Name-only rename cannot change UUID, Ledger,
or lifecycle. Rename and archive serialize on the Participant so concurrent
changes preserve both results.

Errors use `application/problem+json`. A missing or non-owned Ledger returns
404 `finance_ledger_not_found`; a Participant missing from an owned addressed
Ledger returns 404 `finance_participant_not_found`, including IDs belonging to
another Ledger of the same owner. Invalid inputs return 422 `validation_error`.
Existing 403 access, 500 unexpected-error, and 503 database-unavailable semantics
apply.

These are metadata operations with no Account, Transaction, Money, invitation,
identity-provider, or sharing effects. They do not extend the five existing
Finance v1 create submission operations or their versioned evidence protocol.
Participant create returns the resource directly, without submission headers or
a retained submission receipt. A repeated POST creates a distinct identity;
clients must not infer idempotency or silently retry an uncertain create.

## Consumer handoff and later verification

The backend-generated `openapi/openapi.json` is the contract authority. Once
producer P has been independently reviewed, approved, committed, and delivered,
the frontend must synchronize that committed contract through `pnpm api:update`,
then consume the generated Fetch/Query/Zod artifacts. There is no producer commit
identity until that separate delivery stage.

The frontend portion still needs routed accessible create/list/inspect/rename/
archive controls, pending/error behavior, keyboard/focus and applicable axe
verification. It must use UUID identity, retain archived attribution, and filter
archived Participants out of future new-Advance choices. It must not treat this
metadata create response as a Finance submission receipt.

T01 verifies stable identity and unchanged existing Finance v1 financial results
before any Advance, Obligation, Settlement, credit, or Correction exists. Later
financial tickets must verify their own retained historical attribution and
resolution after rename/archive, and reject archived identities for new Advances.
No such financial workflow is implemented by this producer stage.

Issue #27 remains open until both producer P and consumer C are delivered and
pair verification is recorded. Backend-only publication must omit issue linkage,
as specified in the issue's cross-repository delivery procedure.
