# Finance create-submission protocol

Status: approved architecture; implementation specification; not implemented.

Decision baseline: confirmed Q1-Q29 cross-repository design. This document is
the single authoritative specification for this change. Tracker issues and the
frontend authority index point here; they must not maintain competing copies.

## Problem Statement

A Finance create can commit while its response is lost. Quick Entry currently
presents this as a failure and permits another create from the retained draft.
That second request receives a different resource identity and duplicates the
financial event. Account creation can similarly duplicate an Opening Balance.
Ledger and Category creates also need unambiguous submission recovery; name
uniqueness is not submission identity, particularly after renaming.

The invariant is one committed outcome per logical submission, not one event
per unique collection of field values. Two intentional identical entries must
remain possible. Disabling a button, searching for matching transactions, or
retaining an in-memory flag cannot establish correctness.

## Solution

Give each logical create a client-generated opaque submission identity, prepare
it durably in the browser, and bind it durably in PostgreSQL before execution.
The backend serializes identical submissions and commits their financial
changes and immutable terminal receipt together. Retry returns that original
outcome. Recovery looks up a known identity without executing it and offers an
explicit retry of the original command when the outcome remains unresolved.

This is a Finance-specific command protocol, not a generic distributed task
framework. It does not promise that every submission eventually executes.

### Authority and compatibility

Use the [domain glossary](../CONTEXT.md), [Local User identity ADR](adr/0001-local-user-identity-boundary.md),
[OpenAPI ownership ADR](adr/0002-backend-owned-openapi-contract.md), and Finance
ADRs [0003](adr/0003-own-finance-ledgers-by-local-user.md),
[0004](adr/0004-use-an-atomic-transfer-aware-ledger.md),
[0005](adr/0005-derive-account-relative-balances.md), and
[0006](adr/0006-denominate-each-finance-account-in-one-currency.md).
There is no change to their identity or economic meanings.

The [Finance v1 Product and API Specification](finance-v1-product-api.md)
continues to own existing business semantics. This specification deliberately
supersedes its five create response shapes and adds the required submission
protocol. Other operations retain their contracts.

The frontend Finance interaction specification continues to own ordinary
workflows and visual authorities. This specification supersedes its create
failure/retry behavior and adds persistent submitted recovery records. Its
temporary, never-submitted draft lifecycle remains unchanged. All statements
here describe required future behavior, not capabilities already deployed.

## User Stories

1. As a Local User, I want a lost create response to remain an unknown outcome,
   so that I am not encouraged to create a duplicate.
2. As a Local User, I want to retry the exact original submission, so that an
   earlier commit returns its outcome without another financial change.
3. As a Local User, I want to intentionally record identical entries, so that
   legitimate repeated transactions are not suppressed.
4. As a Local User, I want recovery after navigation, reload, and browser
   restart, so that a page's lifetime does not determine financial correctness.
5. As a Local User, I want unresolved creates visible outside their original
   form, including onboarding, so that closing a dialog does not lose recovery.
6. As a Local User, I want to continue unrelated Finance work, so that one
   unresolved create does not block the entire application.
7. As a Local User, I want post-dispatch edits separated from the submitted
   command, so that retry cannot silently submit different content.
8. As a Local User, I want an old create replay to remain safe after editing or
   deleting its resource, so that recovery cannot recreate deleted events.
9. As a Local User, I want Balance Adjustment noChange remembered, so that a
   retry cannot calculate a new adjustment against later balances.
10. As a Local User, I want business rejection outcomes remembered, so that an
    old command cannot unexpectedly succeed after circumstances change.
11. As a Local User, I want explicitly proven invalid commands to be correctable,
    so that ordinary validation does not strand the recovery record forever.
12. As a Local User, I want uncertainty retained for generic errors and absent
    lookups, so that weak evidence cannot authorize an accidental second create.
13. As a Local User, I want duplicate retries from different tabs to be safe,
    so that correctness does not depend on exclusive tab ownership.
14. As a Local User, I want storage failures reported before initial dispatch,
    so that creates are not sent without their recovery context.
15. As a Local User, I want API and user changes to isolate recovery records,
    so that another account or deployment cannot execute my prepared command.
16. As a Local User, I want a committed outcome kept separate from refresh
    failure, so that a failed query cannot reopen a successful create.
17. As a Local User, I want safe acknowledgement and cleanup of resolved local
    records, so that recovery need not become a permanent browser history.
18. As an operator, I want a coordinated cutover and enforcement-preserving
    rollback, so that old requests cannot bypass the new correctness boundary.

## Implementation Decisions

### 1. Invariants and implementation latitude

The behavioral, wire, persistence, ordering, and transition requirements below
are normative. Named record fields are logical fields; database column spelling,
indexes beyond required uniqueness, component filenames, and transaction helper
layout are implementation details. Existing Finance API, service, PostgreSQL,
generated-client, form, Query, and browser test boundaries must be reused.

Keep exact Money, Account Nature signs, one atomic transfer, complete Category
Allocations, derived balances, civil dates, Ledger isolation, archived-reference
rules, adjustment expected-balance/nature checks, and existing edit/delete
lifecycle behavior. Opening Balance is not a Transaction. Do not broaden
currency support, round invalid amounts, or shrink the accepted Money range.

Router owns navigation. Query remains the resource-state cache. The browser
submission journal owns immutable client commands and recovery evidence, not
copies of Accounts, Transactions, balances, or other Query-managed resources.

### 2. Covered operations and entry points

Paths below are relative to the existing `/api` server base; OpenAPI paths must
not duplicate that prefix. Existing create operation IDs remain unchanged.

| Operation / receipt `operation` | POST path                                         | Created resource type                      | Success status |
| ------------------------------- | ------------------------------------------------- | ------------------------------------------ | -------------- |
| `createFinanceLedger`           | `/finance/ledgers`                                | `ledger`                                   | 201            |
| `createFinanceAccount`          | `/finance/ledgers/{ledgerId}/accounts`            | `account`                                  | 201            |
| `createFinanceCategory`         | `/finance/ledgers/{ledgerId}/categories`          | `category`                                 | 201            |
| `createFinanceTransaction`      | `/finance/ledgers/{ledgerId}/transactions`        | `transaction`                              | 201            |
| `createBalanceAdjustment`       | `/finance/ledgers/{ledgerId}/balance-adjustments` | `transaction`, or no resource for noChange | 200            |

Coverage includes first-Ledger onboarding, additional Ledger creation, Account
and Category forms, Quick Entry, full Income/Expense forms, Internal Transfer,
and Balance Adjustment creation. No covered entry point may bypass preparation.

Edit, ordinary replacement, Balance Adjustment replacement, delete, archive,
and unarchive are excluded. Their existing semantics remain; this work makes no
claim to solve their ambiguous outcomes. Preserving create evidence through
those operations is nevertheless required.

### 3. Identity, headers, and user binding

The client generates a cryptographically random UUID (UUIDv4) once per logical
submission, before durable preparation and before dispatch. It is independent
of resource IDs, time, and content. Store the exact submission identity; retries
never regenerate it. The backend never supplies a fallback identity.

All five POSTs require:

| Header                     | Value and meaning                                           |
| -------------------------- | ----------------------------------------------------------- |
| `Idempotency-Key`          | Client-generated submission UUID                            |
| `Finance-Command-Version`  | Explicit version string; initial version is `1`             |
| `Finance-Submission-Owner` | Prepared Local User UUID, asserted against the Current User |

Header names use ordinary case-insensitive HTTP semantics. Reject missing,
malformed, or ambiguous repeated values before admission or financial execution.
The owner header is never an authorization credential. Use the existing identity
resolution and active-user boundary; do not introduce placeholder authentication
or implement deferred Keycloak work. Current-user identity comes from the
existing current-user API, not names, emails, or browser storage alone.

PostgreSQL enforces a unique key on `(localUserId, submissionId)` across all five
operations. Different users have distinct namespaces. The operation and target
Ledger are command content, not extra columns in a narrower uniqueness key.
Reusing a key within one user for another operation or Ledger conflicts after
the required access checks. Do not reveal inaccessible scope through conflicts.

### 4. Versioned canonical command

The stored command has logical members `commandVersion`, `operation`,
`targetLedgerId`, and `body`. Ledger creation has `targetLedgerId: null`; nested
creates retain their original Ledger UUID. Owner/key are the enclosing binding.
These fields describe the original request, never its resource's later state.

Version 1 canonicalization is deterministic and independent of mutable resource
state. Parse using public request aliases, reject extra business fields, and
retain every meaningful field. The command field inventory is:

| Command            | Canonical body fields                                                                                      |
| ------------------ | ---------------------------------------------------------------------------------------------------------- |
| Ledger / Category  | `name`                                                                                                     |
| Account            | `name`, `nature`, `currency`, `openingBalance`, `trackingStartDate`                                        |
| Income / Expense   | `kind`, `accountId`, `transactionDate`, `economicAmount`, complete `categoryAllocations`, `note`           |
| Internal Transfer  | `kind`, `sourceAccountId`, `destinationAccountId`, `amount`, `transactionDate`, `note`                     |
| Balance Adjustment | `accountId`, `transactionDate`, `expectedDerivedBalance`, `expectedAccountNature`, `targetBalance`, `note` |

Rules:

- Ignore JSON object property order; retain array order and cardinality. Do not
  invent allocation sorting or future split semantics.
- Normalize UUID values to their canonical text representation. Preserve exact
  ISO civil dates and enum meaning; do not apply timezone conversion.
- Trim names according to current backend normalization and retain display
  case. Existing case-folded uniqueness is a business rule, not permission to
  equate differently cased display names in a command.
- Trim notes and map blank/omitted notes to null. Apply existing schema defaults,
  including omitted `categoryId` becoming null. Do not equate omitted required
  fields with defaults that the contract does not define.
- Validate Money as a strict plain decimal string plus currency. Use exact
  decimal/string processing, never JavaScript Number or binary floating point.
  Freeze version 1 currency scales (CNY/USD 2, JPY 0) and normalization rules.
  Canonical valid amounts have the catalog scale, insignificant leading zeros
  removed, and negative zero normalized to zero. Valid `10`, `10.0`, and `10.00`
  in CNY therefore match. Excess precision remains invalid even if rounding or
  trimming it would yield a valid value.
- Retain the current 131,072 significant integer-digit durable Money limit;
  never impose a shorter canonical-record or digest-input limit accidentally.
- Preserve positivity, allocation equality, Account currency correspondence,
  transfer roles, and adjustment concurrency inputs. Do not recompute expected
  balances/nature or replace submitted target values during retry.

Store the canonical command itself, not only a hash. Equality is structural
equality of this versioned representation. Raw JSON hashing, similarity matching,
and comparing the created resource's current state are forbidden.

A malformed request does not become an admissible canonical command. Q29's
attempt evidence, described below, is deliberately separate from canonical
admission equality; it permits correlation even when Money or another command
field cannot pass canonical-command validation.

### 5. Backend durable schema and states

Add one Finance-owned submission relation with these logical fields:

| Field               | Requirement                                               |
| ------------------- | --------------------------------------------------------- |
| `localUserId`       | Non-null Local User UUID                                  |
| `submissionId`      | Non-null submission UUID; unique together with Local User |
| `commandVersion`    | Non-null immutable version identifier                     |
| `canonicalCommand`  | Non-null immutable structured canonical command           |
| `retentionLedgerId` | Nullable Ledger UUID; retention/access association        |
| `admittedAt`        | Non-null database admission timestamp                     |
| `terminalOutcome`   | Nullable immutable created/noChange/rejected outcome      |
| `resolvedAt`        | Nullable terminal completion timestamp                    |

Use timezone-aware timestamps with UTC wire representation. Outcome and resolved
timestamp must both be null or both be non-null. Operation, scope, version, and
outcome combinations must be consistent. Created outcomes require a resource
type/UUID; noChange must not fabricate a Transaction identity. Rejected outcomes
must contain stable Problem Details evidence, not partial financial changes.
For nested creates, retention Ledger equals the command's target in both states.
For Ledger creation it is null until created, then equals the created Ledger ID;
rejected Ledger creation leaves it null. The enclosing version must equal the
canonical command version, and every associated Ledger must belong to the owner.

There are exactly two durable states, derived without another running flag:

| Before               | Trigger                                              | After / effect                                                            |
| -------------------- | ---------------------------------------------------- | ------------------------------------------------------------------------- |
| No record            | Valid admission commits                              | Bound unfinished                                                          |
| Bound unfinished     | Successful financial execution commits with receipt  | Terminal created                                                          |
| Bound unfinished     | Adjustment noChange commits with receipt             | Terminal noChange                                                         |
| Bound unfinished     | Business rejection commits after financial rollback  | Terminal rejected                                                         |
| Bound unfinished     | Crash, lost connection, infrastructure failure, busy | Bound unfinished, unless another execution has already committed terminal |
| Terminal             | Authorized identical replay                          | Same terminal evidence; no execution                                      |
| Either durable state | Conflicting reuse                                    | Same record; nonterminal protocol conflict                                |

No record is not a third durable state. Terminal outcomes are variants of the
single terminal state. Never persist execution attempts, worker ownership,
leases, full resource snapshots, or a separate running state. Terminal-to-
unfinished, terminal-to-another-outcome, and command rebinding are forbidden.

### 6. Admission, replay, execution, and contention ordering

The authoritative flow for a create is:

1. Resolve the Current User and require Active status. Validate required protocol
   headers and match the owner assertion. Enforce current authorization for the
   requested scope before revealing submission information.
2. Validate operation/version and immutable request validity using the retained
   version-specific parser/canonicalizer. Protocol version selection may consult
   authorized binding metadata: if a syntactically valid requested version differs
   from an existing binding, return version mismatch without interpreting the new
   version's body. With no binding, an unsupported version remains unsupported.
   A potentially qualifying Q29 rejection follows section 9, not ordinary admission.
   Malformed requests otherwise return normal Problem Details without a receipt.
3. Look up the user/key binding for canonical comparison. Before any metadata or
   outcome disclosure, including the version check above, recheck authorization
   to its stored/retention Ledger. This includes a Ledger created by an earlier
   successful Ledger-create command.
4. Compare the original operation, scope, and canonical body. Version mismatch
   and different-content reuse have distinct protocol conflicts. Do not run
   mutable business checks to decide replay.
5. If terminal and identical, return the original receipt/status. If unfinished
   and identical, continue to execution. If absent and the version allows new
   admission, insert the binding and commit this short admission transaction.
6. Execute in a separate transaction, serializing on the submission row. Re-read
   its state after obtaining serialization; a now-terminal record is replayed.
   Recheck applicable access before executing. Apply current mutable business
   validation and existing financial/account locks in their compatible order.
7. Commit the complete financial changes and terminal success evidence together.
   For a recognized business rejection, roll back every tentative financial
   change before committing its stable rejection outcome. Infrastructure or
   unknown failures do not become business rejection receipts.

The uniqueness constraint arbitrates admission races, including different
content. Losing an insert race must read the winning binding and compare/replay;
it must not execute another create. Bound waits apply to admission uniqueness
contention as well as execution row-lock contention. A wait limit returns
`409 finance_submission_busy` with no receipt. Roll back that request's incomplete
transaction; do not erase a previously committed binding. No infinite waits or
automatic client resubmission loop.

Covered financial service calls must participate in the execution transaction;
an existing helper that commits independently must not commit financial changes
before receipt persistence. Preserve rejection rollback through a transaction
or savepoint boundary that leaves no partial financial writes before the
submission's rejection is committed.

Use PostgreSQL transaction serialization, not an application precheck alone.
No worker recovery process is needed: explicit identical retries resume bound
unfinished commands. Account/domain locks continue to protect different
submissions that affect the same financial resources. Avoid reversing existing
Account lock ordering while adding the submission lock.

The durable admission transaction contains no financial changes. A committed
create outcome cannot exist without its receipt, and a success receipt cannot
exist without its corresponding changes or explicit noChange result. If commit
acknowledgement itself is lost, do not infer rollback; the next request reads
the database outcome. No schema projection or response serialization failure may
be relabeled a business rejection after a successful commit.

Authorization, access loss, or infrastructure failure after admission leave the
binding intact. They do not terminalize it. Freeze immutable request validation
per version; mutable checks run only for new/unfinished execution, not terminal
replay. This preserves normal validation without letting an archived Account or
changed balance invalidate a successful original create.

### 7. Immutable receipt and operation-specific constraints

Every terminal receipt has these required members:

| Member           | Meaning                                        |
| ---------------- | ---------------------------------------------- |
| `submissionId`   | Original UUID                                  |
| `commandVersion` | Original version string                        |
| `operation`      | One of the five existing create operation IDs  |
| `targetLedgerId` | Original target UUID; null for Ledger creation |
| `admittedAt`     | Original admission timestamp                   |
| `resolvedAt`     | Terminal completion timestamp                  |
| `outcome`        | Discriminated outcome described below          |

The outcome discriminator is `kind`:

- `created`: required `resource` with `type` and `id`; no rejection fields.
- `noChange`: no resource and no rejection fields; Balance Adjustment only.
- `rejected`: required `problem` containing stable original `type`, `title`,
  `status`, `code`, `detail`, and any defined stable structured error details;
  no resource. Exclude request-specific `instance`, trace IDs, and nested
  receipt/evidence extensions from immutable rejection evidence.

Constrain operation/resource combinations to the table in section 2. Every
operation permits rejected; only Balance Adjustment permits noChange. Generate
discriminated schemas that cannot represent impossible combinations. Receipt
identity and timestamps are original evidence, not refreshed on replay.

Initial success and replay return the receipt directly as JSON with the same
success status (ordinary creates 201; Balance Adjustment 200). Do not return a
current or historical resource snapshot in place of the receipt. Terminal
business rejection returns its original business HTTP status and Problem
Details, extended with required typed `submissionReceipt`. A current `instance`
may describe the current HTTP request outside the immutable receipt.

Replaying noChange never recalculates a delta. Replaying created after edit or
deletion never updates or recreates the resource. Current state comes only from
normal resource/query APIs. A created receipt remains success if those APIs now
report the resource unavailable.

### 8. Response classification and protocol errors

All error responses remain `application/problem+json`. Keep the existing core
Problem Details fields and business codes. The following new protocol codes
make nonterminal failures distinguishable without changing business meanings:

| HTTP | Code                                   | Meaning                                                                    |
| ---- | -------------------------------------- | -------------------------------------------------------------------------- |
| 400  | `finance_submission_protocol_required` | Required protocol header missing; actionable client-update guidance        |
| 400  | `finance_submission_protocol_invalid`  | Malformed/ambiguous header value                                           |
| 403  | `finance_submission_owner_mismatch`    | Owner assertion differs from Current User; no binding or disclosure        |
| 409  | `finance_submission_content_conflict`  | Same user/key bound to different canonical operation/scope/content         |
| 409  | `finance_submission_version_mismatch`  | Same user/key uses a different command version                             |
| 409  | `finance_submission_busy`              | Bounded admission/execution serialization wait expired                     |
| 422  | `finance_command_version_unsupported`  | Cannot interpret the requested version                                     |
| 409  | `finance_command_version_closed`       | Version is interpretable but closed to new admissions; no existing binding |
| 404  | `finance_submission_not_found`         | Lookup absent or inaccessible, indistinguishably                           |

Existing authentication/access and scoped-resource errors keep their existing
codes/statuses and non-disclosure rules. A syntactically valid different version
on an authorized existing binding returns version mismatch, even when that new
version cannot be interpreted. Without a binding, unsupported versions fail
before command interpretation. Neither permits automatic upgrade.
Do not include an existing receipt in content/version conflicts.

| Response/evidence                                                               | Backend terminal?                | Client action                                                  |
| ------------------------------------------------------------------------------- | -------------------------------- | -------------------------------------------------------------- |
| Valid correlated success receipt                                                | Yes                              | Durably resolve locally; reconcile resource queries separately |
| Business Problem Details with valid correlated `submissionReceipt`              | Yes                              | Durably resolve rejected; corrected create needs new identity  |
| Lookup terminal with valid correlated receipt                                   | Yes                              | Same resolution, including lookup of rejected outcome          |
| Valid fully correlated Q29 `commandValidationRejection`                         | No record required               | Durably resolve locally as definitively not admitted           |
| Generic 422 / ordinary `validation_error`                                       | Not established                  | Remain unresolved                                              |
| Busy, infrastructure failure, timeout, lost/malformed response or parse failure | Not established                  | Remain unresolved                                              |
| Access/owner/protocol/version/content conflict                                  | Not established by this response | Preserve record; present relevant blocked/access/unknown state |
| Lookup unfinished, 404, or failed lookup                                        | Not established                  | Remain unresolved; never infer cancellation                    |

HTTP status alone is never terminal evidence. A response may be lost after
commit, and a request may arrive after a lookup reports absence. Optional retry
hints do not authorize automatic execution or new submission identities.

### 9. Q29: definitive pre-admission command-validation rejection

This is not a submissionReceipt or a third backend state. It proves only that
the exact attempted immutable command cannot be admitted under the original
fixed version. Its purpose is to allow correction of invalid commands that
would otherwise remain unresolved forever without a possible receipt.

Use a typed Problem Details extension named `commandValidationRejection`, with:

| Member           | Required value                                         |
| ---------------- | ------------------------------------------------------ |
| `kind`           | `definitivelyNotAdmitted`                              |
| `submissionId`   | Attempted UUID                                         |
| `commandVersion` | Known supported original version                       |
| `ownerId`        | Successfully asserted Current User UUID                |
| `operation`      | Known attempted operation                              |
| `targetLedgerId` | Attempted scope, null for Ledger creation              |
| `attemptedBody`  | Exact parsed JSON request object, before normalization |

The ordinary Problem Details `errors`/details identify the validation failure.
The attempted-body echo is correlation evidence, not an admissible canonical
command, a hash, a stored financial snapshot, or an instruction. Compare it with
the immutable dispatched request structurally, preserving value types, strings,
array order, and presence versus omission; only object key order is irrelevant.
Do not render echoed strings as markup or use them to reconstruct a lost command.
Only emit this extension where exact parsed-object correlation is possible;
malformed JSON or an uncorrelatable body stays an ordinary nonterminal error.

All of these guards are required:

1. Authentication, active-user eligibility, owner assertion, current scope
   authorization, operation recognition, and version support succeeded.
2. A frozen, immutable version-defined command-validity rule is the sole cause.
   An identical logical command could not previously have passed that version's
   admission rules and cannot pass them later. No mutable Account/Category state,
   current balance, access state, or version-support failure supplies this proof.
3. The requested identity is not known to be bound/conflicting, and no evidence
   suggests an earlier identical request may have been admitted or committed.
   Absence is not the proof: deterministic impossibility under the fixed
   validator is. If this cannot be established, omit the extension.
4. The frontend validates the typed extension and matches namespace, owner, key,
   version, operation, scope, and the exact captured immutable attempted body.
   A known conflicting-reuse response blocks this path. Evidence for one body
   must never resolve any different or earlier body/attempt.

Do not create a durable backend record for this rejection. Keep generic
`422 validation_error` behavior unless the extra proof is established. In
particular, a catch-all framework validation handler must not attach this
extension before authentication/authorization or by status-code classification.

After proof, the frontend atomically stores a local definitively-not-admitted
resolution before acknowledgement/cleanup. Corrected content is a new logical
submission with a new key. Busy, unsupported/closed versions, missing/malformed
headers, owner/access errors, infrastructure failures, lookup absence, and
conflicting reuse can never use this extension. A terminal receipt and this
extension must not coexist in one response.

Version compatibility is essential to this proof: never relax an old version's
immutable validity rules and start admitting commands previously proven invalid.
Introduce a new version for changed validity/normalization instead.

### 10. Authenticated lookup

Add operation `getFinanceSubmission`:
`GET /finance/submissions/{submissionId}`. Require a valid
`Finance-Submission-Owner` assertion and the existing active-user boundary.
The UUID is in the path; lookup does not require an Idempotency-Key or command
version header because it discovers the stored version and does not execute.

Look up only within the Current User namespace, then enforce current access to
the stored target/retention Ledger before returning anything. A successful
Ledger create uses its created Ledger for that access check; unfinished or
rejected Ledger creates remain user-scoped. Never disclose canonical content.

Return HTTP 200 with one of two discriminated bodies:

- `state: unfinished`, plus `submissionId`, `commandVersion`, `operation`,
  `targetLedgerId`, and `admittedAt`.
- `state: terminal`, plus `receipt` containing the complete immutable receipt.

A rejected terminal receipt is still HTTP 200 because lookup succeeded. Absence
and inaccessible records use identical safe 404 Problem Details, never different
details/counts that reveal existence. Authentication errors remain access errors.
All lookup responses, including errors, use `Cache-Control: no-store`.

The client performs a fresh network lookup on recovery. Cached unfinished or
absent results cannot establish an outcome. A terminal receipt must match the
local key, version, operation, and target; a conflicting local binding must not
be silently associated with another command's terminal outcome. An unmatched or
malformed receipt enters blocked recovery, not success.

### 11. IndexedDB ownership, preparation, and validation

Use a small Finance-specific IndexedDB journal, not Query persistence or a
generic offline queue. Namespace keys are the resolved, normalized browser-
facing API base URL, Local User UUID, and submission UUID. Resolve relative URLs
against the current origin; normalize trailing-slash joining consistently. Do
not store credentials or use the development proxy's internal upstream URL.

Each record has a separate local schema version, immutable command version,
operation/known relative endpoint, owner, key, target context, immutable submitted
JSON body, prepared timestamp, minimal recovery context, and local state. Validate
all persisted data with Zod before use. Check that operation, endpoint, scope,
and namespace agree; never dispatch an arbitrary persisted URL. Recovery context
may identify the originating workflow/return destination but contains no copied
resource models or mutable business authority.

Preparation is: resolve current identity/API namespace; validate current form;
allocate identity; freeze the submitted command; insert it transactionally;
await the successful transaction completion event; then dispatch from that
persisted immutable command. An individual request's IndexedDB success event is
not enough. If creation/storage fails, send nothing. Once prepared, even a crash
before actual network dispatch is recovered conservatively as unresolved.

Request persistent browser storage where supported. Denial alone does not block
dispatch. Normal same-profile/origin navigation, reload, and restart recovery
are required; eviction, private-session teardown, or external clearing may remove
local evidence. Backend uniqueness survives such local loss. Do not promise
cross-device recovery or recovery after storage removal.

Storage open, migration, validation, and write failures block affected
preparation/recovery. Never auto-clear the database, regenerate keys, guess
missing fields, or silently downgrade a stored version. Handle blocked database
upgrades/version-change events explicitly and instruct stale tabs to reload;
an upgrade must preserve unresolved records. If an identifiable corrupt record
can safely supply a validated lookup identity/namespace, lookup may resolve it;
without sufficient correlation or a terminal receipt, remain blocked.

### 12. Local state transitions and frontend behavior

Local persistent states are distinct from the two-state backend model:

| Local state                                       | Allowed transitions                                                            |
| ------------------------------------------------- | ------------------------------------------------------------------------------ |
| No record                                         | Successful durable preparation -> unresolved                                   |
| Unresolved                                        | Valid correlated terminal receipt -> resolved with receipt                     |
| Unresolved                                        | Fully validated Q29 evidence -> definitively not admitted                      |
| Unresolved                                        | Any nonterminal/uncertain failure -> unresolved, with appropriate presentation |
| Resolved with receipt / definitively not admitted | User acknowledgement after durable resolution -> removed                       |
| Removed                                           | Stale callbacks/retries -> stays removed; optional known-ID lookup only        |

Unknown outcome, access unavailable, incompatible/corrupt data, and persistence
failure are presentation/recovery conditions, not new backend states. No network
error overwrites a resolved local outcome. A conflicting terminal receipt is an
integrity/recovery problem, not permission to replace stored terminal evidence.

On entering Finance, navigating, reloading, or restarting, load the current
namespace's retained records and automatically look up unresolved identities.
Never automatically POST because the browser restarted. Explicit
"Retry original submission" rereads the durable record, confirms the current
identity/API namespace, and resends its original identity, version, scope, and
body. It does not obtain fresh adjustment preconditions, substitute active
resources, or read the current editable form as request content.

Provide a Finance-level recovery entry point visible across destinations and
during first-Ledger onboarding. Dialog closure and Ledger navigation may hide a
form but do not discard submitted recovery. Ledger switching never migrates a
command to the new Ledger. Unresolved creates do not globally block unrelated
Finance work. Intentionally creating another entry is a distinct explicit action
with a new identity, never an automatic fallback from retry.

The submitted content is immutable until resolved. Separate post-dispatch edits
remain temporary draft state. Do not submit those edits as the unresolved request
or silently as a replacement submission. After original success, corrections
use existing edit/delete workflows. After terminal business rejection or Q29
resolution, corrected content can become a newly prepared submission.

When Current User or API base changes, stop displaying/retrying the previous
namespace's records and retain them. Resume only on return to that exact
namespace. If current identity is unavailable, do not prepare or dispatch.
Callbacks from the previous namespace must not update the current user's UI or
resource cache; any local evidence update remains correlated to its original
partition. The owner header guards a switch between identity read and dispatch.

Once a terminal receipt arrives, durably resolve the journal before retiring
pending recovery. Then reconcile the affected normal Query resources. Query
refresh failure is separate feedback and cannot reopen the create. If the local
resolution write fails, preserve pending evidence; later lookup can resolve it
again. Do not overwrite newer edits, steal focus into an unmounted dialog, or
repeat destructive draft clearing when delayed recovery completes.

Preserve Quick Entry's existing successful reset/context/focus behavior for the
original mounted workflow, stale-safe references, Overview/month/selected-day
count authority, history/account reconciliation, replacement session ownership,
and deletion markers. Keep UI focus/announcement behavior accessible. Resource
fetch errors after a receipt must not resurrect deleted identities or seed old
resource snapshots into Query.

### 13. Multi-tab coordination and local cleanup

There is no exclusive tab owner, lease, heartbeat executor, or background retry
worker. IndexedDB transactions arbitrate preparation and monotonic resolution.
Cross-tab notifications (for example BroadcastChannel) only prompt rereading
durable storage. Re-read on normal activation/recovery as well so notification
delivery is not required for correctness.

Multiple tabs may safely retry the same stored identity; PostgreSQL provides
uniqueness. Preparation is insert-only for a new command; retry/resolve are
conditional updates of an existing record, never upserts that recreate one.
Late transport errors cannot reverse terminal resolution.

After resolution has been durably recorded, the user may acknowledge and remove
the local record in a transaction that verifies it is resolved. No automatic
unresolved deletion, TTL, or ordinary discard action. Permanent local history
is not required. Other tabs detect removal by durable reread. A stale tab must
stop retrying if its record is absent and may only look up the known identity;
it cannot mint a new key or reconstruct the pending record. An already-sent
request remains safe through backend idempotency even if local cleanup races
with its response.

### 14. Retention and version support

Nested-create records, including rejection and unfinished records, live for the
target Ledger's lifetime. Successful Ledger-create records live for the created
Ledger's lifetime. Unfinished and rejected Ledger creates have no created Ledger
and remain for the Local User's lifetime. Disabled users retain records. There
is no time-based backend expiration.

Resource IDs in receipts are historical references, not cascading foreign keys
to current Accounts, Categories, or Transactions. Transaction replacement may
physically delete/reinsert its identity; it must not erase or mutate evidence.
Set a Ledger-create retention association in the same transaction as successful
Ledger creation and its receipt. Current Ledger deletion is outside scope; any
future deletion feature must define submission-record handling explicitly.

Versions are part of the immutable binding. Retry uses the original version;
never silently upgrade an unresolved command. Support its parser, canonicalizer,
immutable validity rules, and receipt reading as long as retained submissions
require them. A version may close to new admissions while existing bindings
still replay/resume. Lookup does not execute and can return their stored evidence.
Local journal schema migrations are separate from command-version changes.

### 15. Responsibility and generated contract boundaries

Backend Finance owns validation, canonicalization, durable uniqueness, execution
serialization, atomic outcomes, access control, receipts, retention, and lookup.
The browser owns identity generation, durable preparation, explicit retry intent,
correlation, local recovery presentation, and safe acknowledgement. Browser state
never authorizes uniqueness or substitutes for backend financial validation.

Make the deliberate breaking request/response changes in backend schemas and
export backend-owned OpenAPI deterministically. Document all required headers,
operation-specific receipt unions, typed Problem Details extensions, lookup
responses, and nonterminal errors. Runtime behavior and declared schemas must
agree, including errors produced before route execution.

Commit/validate the backend contract in the separately authorized delivery phase,
then synchronize the frontend snapshot and regenerate Fetch, Query, Zod, MSW,
and fixtures with the existing workflow. Never hand-edit generated files or
introduce a parallel handwritten Finance fetch client. Parse both successful
and thrown error bodies with generated schemas before interpreting evidence;
generated Fetch non-OK errors currently travel through their error-info path.
Preserve required-only-anyOf handling and other existing generator safeguards.

No global Query defaults change. Submission lookup uses a fresh read and local
query behavior as needed; do not copy general server state into IndexedDB or a
new global store. Normal builds/tests remain offline and do not regenerate the
contract implicitly.

### 16. Migration, cutover, and rollback

Add an explicit PostgreSQL migration for the Finance submission relation,
user/key uniqueness, state consistency, and necessary ownership/retention
associations. It creates no fabricated historical records, seeds no users, and
does not run implicitly at application startup. Do not add SQLite support or a
generic idempotency framework. Verify safe schema application to a populated
Finance database without changing existing financial data.

Use this coordinated breaking cutover:

1. Prepare and validate migration, backend behavior/OpenAPI, generated frontend,
   browser recovery, and the regression scenarios below.
2. Close admission of all five Finance creates at the serving boundary. Keep
   this closure effective throughout the mixed-version interval.
3. Drain previously accepted requests and prevent old backend instances from
   executing further creates, including old keep-alive/queued requests.
4. Resolve outstanding old database transactions before proceeding. Establish
   whether each finished or rolled back; a lost client response is not rollback.
5. Apply the additive migration and deploy the enforcing backend everywhere.
6. Publish the matching frontend/generated contract.
7. Verify every serving backend rejects unkeyed creates before financial changes,
   then reopen create admission. Delayed stale-client requests fail safely with
   actionable update-required feedback.

The application need not gain a generic feature-flag platform for this window.
The deployment owner must identify the actual ingress/instance drain mechanism
before cutover; deferred APISIX work is not silently treated as already deployed.

Once keyed submissions exist, rollback must preserve their data, parsers,
receipts, lookup capability, and idempotency enforcement. Do not revert to an
unkeyed backend or destructively downgrade the submission table. If a safe
compatible rollback is unavailable, keep creates closed and repair forward.
Ticket slicing or integration branches must not expose partially covered creates
as the completed protocol; public activation waits for all five paths.

Historical resources cannot be assigned reliable submission identities from
content. Historical ambiguous requests require reconciliation before another
submission; generating a new key cannot protect a previous unkeyed commit.
Do not deduplicate legitimate historical transactions or automatically rekey an
old pending draft after an update.

## Testing Decisions

### Existing seams and evidence

Use behavioral tests at the highest applicable existing boundaries:

- Backend Finance HTTP/API integration tests with real protected PostgreSQL prove
  ownership, uniqueness, transaction atomicity, contention, crash/retry, and
  resource effects. Existing Finance API/PostgreSQL tests already cover transfer
  atomicity, projection rollback, replacement identity, ownership, and lock order.
  Mocked HTTP alone cannot prove the database invariant.
- Frontend generated-handler/component tests prove request immutability,
  response/error discrimination, draft/focus behavior, and Query reconciliation.
  Existing Finance dialog and lifecycle tests provide prior art.
- Production-preview Chromium tests exercise real IndexedDB, navigation/reload,
  multiple pages in one browser profile, persistent-profile restart, keyboard
  recovery, and applicable axe checks. MSW/interception may supply protocol
  responses for UI tests but must not be presented as proof of backend commit.
- One integrated lost-response scenario must combine a real backend commit with
  client-visible response loss and inspect final database financial effects.
  Use a test transport/interception seam after server completion, not a fake
  pre-commit network failure. Fault injection must not become a production API.

Use synchronization/barriers and observable committed outcomes for concurrency
and fault tests, never timing sleeps or retries that hide nondeterminism. Verify
effects, receipts, access boundaries, and recovery actions, not internal helper
call counts. Use only the protected dedicated test database, never production
database fallback. Full implementation validation uses each repository's normal
checks and the backend protected harness; browser checks retain unexpected
console/page-error failure and applicable accessibility requirements.

### Acceptance criteria and regression scenarios

Each row is a required observable result, not a suggestion to add isolated tests
that mirror implementation. Cover all five endpoints and every frontend entry
point; use matrices where semantics are shared.

| ID   | Scenario                                                                    | Required result                                                                                      |
| ---- | --------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| AC01 | Commit succeeds, response lost, identical retry                             | One created resource/economic effect; original receipt and success status replayed                   |
| AC02 | Concurrent identical first submissions                                      | One binding and one outcome; loser replays or gets nonterminal busy, never a second effect           |
| AC03 | Same user/key, changed valid content                                        | Content conflict; original command/evidence/effects unchanged                                        |
| AC04 | Same key, different command version (including an unknown version)          | Version-mismatch conflict; no reinterpretation or auto-upgrade                                       |
| AC05 | Same user/key, different operation or accessible Ledger                     | Content conflict across the common namespace; no new effect                                          |
| AC06 | Same UUID used by different Local Users                                     | Independent namespaces, existing authorization and no cross-user disclosure                          |
| AC07 | Crash after admission before execution                                      | Bound unfinished survives; explicit identical retry executes at most once                            |
| AC08 | Failure after financial writes but before atomic outcome commit             | No partial financial changes/terminal success; binding remains recoverable                           |
| AC09 | Commit acknowledgement lost                                                 | Lookup/retry observes actual DB state; no assumed rollback or second execution                       |
| AC10 | Terminal replay after resource edit or internal delete/reinsert replacement | Original receipt unchanged; current state fetched separately                                         |
| AC11 | Terminal replay after resource deletion                                     | Original receipt survives; resource never recreated                                                  |
| AC12 | Adjustment noChange, later balance changes, retry                           | Original noChange; no new Transaction or recalculated delta                                          |
| AC13 | Terminal business rejection, later state becomes eligible                   | Original rejection/status/details replayed; corrected request requires new key                       |
| AC14 | Q29 eligible immutable invalid command                                      | Typed correlated evidence; no backend record/effects; durable local not-admitted resolution          |
| AC15 | Generic 422 / bare validation_error                                         | Remains unresolved; no new key or cleanup based on status                                            |
| AC16 | Q29 metadata/body mismatch, mutable failure, or known conflict              | Evidence cannot resolve local submission; no automatic correction/resubmission                       |
| AC17 | Admission uniqueness wait exceeds limit                                     | Nonterminal busy within bound; no partial binding overwrite or financial effect                      |
| AC18 | Execution wait exceeds limit / infrastructure timeout                       | Same identity retained; no terminal receipt or automatic POST loop                                   |
| AC19 | Lookup absent while original request is delayed                             | Remains unresolved; delayed commit and later retry still converge                                    |
| AC20 | Lookup unfinished / failed / inaccessible                                   | No execution, no command content disclosure, unknown outcome retained                                |
| AC21 | Lookup terminal rejection                                                   | HTTP 200 with rejected receipt; local resolution, not a failed-GET interpretation                    |
| AC22 | Receipt correlation mismatch or known content conflict followed by lookup   | No false success for the wrong local command                                                         |
| AC23 | Authentication disabled/access lost after admission                         | No disclosure/execution; binding/outcome preserved for authorized recovery                           |
| AC24 | User/API namespace switch, including in-flight callbacks                    | Prior records retained but hidden/not retried; wrong-owner dispatch rejected                         |
| AC25 | IndexedDB preparation fails or migration is blocked                         | No initial network create; actionable failure, no automatic clear                                    |
| AC26 | Durable preparation completes, browser crashes before POST                  | Record recovered as unresolved; lookup then explicit retry only                                      |
| AC27 | Navigation/reload/profile restart                                           | Same stored key/version/body; automatic lookup, no automatic create                                  |
| AC28 | Two tabs retry one record                                                   | Backend effect once; local resolution monotonic                                                      |
| AC29 | Terminal receipt followed by refresh failure                                | Locally resolved; separate refresh error; no recreated resource or retry-as-new                      |
| AC30 | Local resolution write fails                                                | Do not retire pending evidence; later lookup safely resolves again                                   |
| AC31 | User acknowledges resolved record, stale tab callback/retry arrives         | Record stays removed; no upsert/new key; optional lookup only                                        |
| AC32 | User edits after dispatch                                                   | Submitted command unchanged; edited transient draft never silently reused                            |
| AC33 | Explicit intentional identical additional entry                             | Fresh key and another legitimate resource/economic effect                                            |
| AC34 | Canonical equivalents and meaningful differences                            | Valid Money/default/note/key-order equivalents match; changed meaningful fields conflict             |
| AC35 | Money boundaries, large values, negative zero, precision, nature signs      | Exact existing semantics/range; no float/rounding/truncation; invalid precision stays invalid        |
| AC36 | Old version closed to new admission                                         | Existing binding replay/resume/lookup supported; absent key fails nonterminal closed-version error   |
| AC37 | Old validator evolution and Q29 proof                                       | No old-version relaxation that could admit a command previously proven invalid                       |
| AC38 | Stale frontend missing headers                                              | Update-required protocol error before any financial write or binding                                 |
| AC39 | Unsupported/malformed/ambiguous headers                                     | No fallback key, Q29 evidence, terminal receipt, or execution                                        |
| AC40 | Retention across editing/deletion/disablement                               | No TTL/cascade loss; original evidence remains under agreed lifetime                                 |
| AC41 | Five endpoint response/schema matrix                                        | Correct resource union/status; noChange only adjustment; no invalid union accepted                   |
| AC42 | Lookup caching/non-disclosure                                               | no-store on success/errors; absent/inaccessible indistinguishable; fresh recovery request            |
| AC43 | Populated DB migration and interrupted cutover                              | Existing resources untouched; creates remain closed until universal enforcement                      |
| AC44 | Enforcement-preserving rollback                                             | Existing keyed evidence survives; unkeyed backend is never reopened                                  |
| AC45 | Historical ambiguous unkeyed request                                        | No fabricated key, content deduplication, or automatic retry as a new submission                     |
| AC46 | Existing Finance lifecycle/keyboard/axe suite                               | Draft, focus, count, cache, stale-reference, replacement, and terminal deletion invariants preserved |

## Out of Scope

- Idempotency for edit, replacement, delete, archive, or unarchive commands.
- Cross-device submission discovery or recovery after browser storage removal.
- Permanent browser submission history or persistence of ordinary/post-dispatch drafts.
- Content-based transaction deduplication or historical submission backfill.
- Event sourcing, append-only bookkeeping, distributed workers, leases, queues,
  general offline execution, a global idempotency framework, or new global state.
- Keycloak integration, general deployment infrastructure redesign, future Ledger
  deletion semantics, new currencies/FX, or changes to financial domain meaning.
- Production code, schema migration files, generated contract edits, feature
  implementation, commits, or deployment as part of this specification task.

## Further Notes

### Decision traceability

| Confirmed decisions | Specification coverage                                                                      |
| ------------------- | ------------------------------------------------------------------------------------------- |
| Q1-Q2               | Immutable submitted content; retention and deletion-safe evidence                           |
| Q3-Q5               | Database authority/atomicity; conflicts; durable browser preparation                        |
| Q6-Q10              | Five-create scope; random identity; receipts; canonicalization; terminal business rejection |
| Q11-Q17             | User namespace; admission; access/replay; recovery; drafts; headers; breaking cutover       |
| Q18-Q22             | Exact backend record/states; versioning; recovery failures; local cleanup; rollout          |
| Q23-Q28             | Wire receipts/errors; lookup; IndexedDB; multi-tab; owner assertion; bounded contention     |
| Q29                 | Definitive invalid-command evidence, without a backend submission record                    |

### Implementation facts to establish in tickets

The architecture has no unresolved product decision. Before implementation or
deployment can claim completion, establish these environment/mechanism facts:

- Select and measure finite admission/execution lock-wait budgets in the actual
  PostgreSQL/driver configuration; test both waits and transaction cleanup.
- Validate the error boundary can emit Q29 evidence only after its access and
  immutable-validation guards; retain generic unresolved errors otherwise.
- Validate generated operation-specific receipt/error unions and required header
  arguments against the pinned toolchain without manual generated edits.
- Prove IndexedDB migration/failure and persistent-profile restart behavior in
  the existing Chromium harness; do not silently promise untested browsers.
- Identify the real serving-boundary closure, old-instance drain, outstanding
  transaction inspection, and enforcement-preserving rollback mechanisms for
  the deployment environment. They are cutover prerequisites, not permission
  to add speculative infrastructure.

This specification is ready for `to-tickets`. Tickets must carry the relevant
acceptance IDs and cross-repository dependencies, and preserve the common final
activation gate. Ticket creation and implementation remain separate steps.
