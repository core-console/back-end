# Finance submission application qualification (T11)

The [protocol](finance-create-submission-protocol.md) remains normative. This
index reconciles application evidence for backend issue #24 with the completed
five-operation consumer. It neither changes the protocol nor opens the common
T13 activation gate. Operational AC43–AC45 evidence must still join T12 at T13.

## Reproducing the qualification

Use the backend's pinned Python/uv toolchain and the frontend's pinned Node/pnpm
toolchain. Set only the existing dedicated, guarded `TEST_DATABASE_URL`; never
substitute the development database. Both checkouts must contain the reviewed,
matching producer/consumer contracts. The browser test requires a clean delivered
frontend, compares parsed OpenAPI snapshots, and records both revisions.

1. In the frontend checkout run `pnpm check`, then `pnpm e2e -- --retries=0`.
2. In the backend environment set `FINANCE_SUBMISSION_FRONTEND` to the absolute
   frontend checkout path. This explicitly opts into real browser qualification.
3. Run `uv run --frozen python scripts/agent_harness.py validate --base <base-sha>
--protected`, followed by `review-state` with that same base.

The protected fixture validates the dedicated database before resetting/migrating
it. The browser test uses that already protected target and cleans its actor/data
through the existing PostgreSQL session fixture. It starts Uvicorn and the
frontend's normal Vite production preview on loopback ephemeral ports. No
production endpoint, configuration flag, dependency, generated client change,
database bypass, or application retry mechanism is introduced.

`tests/integration/test_finance_submission_browser.py` invokes
`tests/browser/finance-submissions.mjs`. Without the explicit frontend setting it
skips, so ordinary backend tests do not acquire a Node/Chromium dependency. A
skip is **not** T11 qualification: the required protected run must include this
test and its eleven successful scenarios. An opted-in missing build, dirty
consumer, differing contract, browser error, or failed database assertion fails.

The test writes `.agent/t11-browser.json` and `.agent/t11-node.log`. The JSON
records producer/consumer revisions, Python, Node, Chromium, PostgreSQL version
and database name, original receipts/statuses, exact dispatched bodies/headers,
and independently queried committed rows before loss and after mutation. The
backend harness separately binds the full candidate and validation outcome to
its snapshot digest; these diagnostic files do not authorize delivery. Retain
the frontend check/Chromium logs alongside the protected receipt when reviewing.
Do not infer a current result from this index without the matching run evidence.

## What establishes durable commit

Every business request from the production browser reaches the real backend.
After the first create completes, the transport reads the original terminal
HTTP response. Before aborting delivery to the application, it
calls an observation route installed **only in this isolated test app**. That
route reads PostgreSQL using a separate session/connection, checks the actor's
submission, and returns its committed terminal tuple plus financial rows.

This observation cannot see the producer request's uncommitted changes. The
test verifies the original receipt against the observed outcome and resolved
timestamp before aborting the browser transport. It does not inject an error before
execution or infer commit from a mocked receipt. To leave explicit retry
available, selected real terminal lookup responses are also made unusable at
the transport boundary; no absent/unfinished/terminal response is fabricated.
Additional Ledger recovery instead consumes the real automatic terminal lookup.

Browser observations prove preparation before dispatch, immutable key/owner/
version/path/body, unknown-outcome presentation, reload without automatic POST,
keyboard retry, journal resolution/acknowledgement, and settled-render axe checks.
Only the independent SQL observations establish durable outcome visibility,
Opening Balance, Transaction/Movement/Allocation counts and exact amounts, and
absence of duplicated or recreated financial effects. Neither browser storage
nor HTTP resource lists substitute for that database evidence.

Unexpected console errors and all page exceptions fail. The only expected HTTP
console errors are exact URLs/messages for the committed request's injected
transport abort, an independently verified deleted Transaction's 404, or the
original terminal rejection's replay status. They are
recorded explicitly; no blanket status/error suppression is used. Accessibility
checks await fonts and finite rendering animations, without disabling axe rules
or excluding elements. There are no timing sleeps or transport/test retries.

## Real browser operation/outcome matrix

Every row loses its first usable response **after SQL observes commit** and
compares original lookup/retry evidence and final PostgreSQL effects.

| Scenario                | Original outcome/status and database assertion                                                                         | Later mutable change and recovery                                                                                 |
| ----------------------- | ---------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| First Ledger onboarding | created / 201; one Ledger and original terminal binding                                                                | Rename; keyboard retry returns original receipt                                                                   |
| Additional Ledger       | created / 201; one additional Ledger                                                                                   | Rename; automatic browser lookup resolves with no POST; identical HTTP retry replays                              |
| Account                 | created / 201; one additional liability Account, exact Opening Balance `-9007199254740993.01`, no Transaction/Movement | Rename; original retry retains position and receipt                                                               |
| Category                | created / 201; one Category                                                                                            | Rename/archive; retry does not recreate or reactivate it                                                          |
| Income                  | created / 201; one Transaction, `12.30` Movement and Allocation                                                        | Delete Transaction; original replay does not recreate any rows                                                    |
| Expense                 | created / 201; one Transaction, `-12.30` Movement, `12.30` Allocation                                                  | Two tabs submit the original key behind a barrier; both replay, journal resolves monotonically, effects stay once |
| Internal Transfer       | created / 201; one Transaction, source `-12.30`, destination `12.30`, no Allocation                                    | Delete; replay does not recreate either leg                                                                       |
| Adjustment created      | created / 200; one Transaction/Movement `12.30`, no Allocation                                                         | Delete; replay remains original created success                                                                   |
| Adjustment noChange     | noChange / 200; no Transaction/Movement/resource identity                                                              | Later adjustment changes balance by `9.99`; retry remains original noChange and adds nothing                      |
| Transaction rejected    | rejected / 409; archived Account, no financial rows                                                                    | Unarchive; retry retains original rejection/status/details                                                        |
| Adjustment rejected     | rejected / 409; stale expected balance; only the independent intervening adjustment exists                             | Delete intervening adjustment, restoring eligibility; original rejection still replays with no effect             |

After the Expense is acknowledged, a deliberate identical create through the
real form uses a fresh key and produces a different Transaction: SQL observes
exactly two Transactions, two `-12.30` Movements and two `12.30` Allocations.
Both browser retries are observed behind a common barrier before forwarding
them to the real backend. BroadcastChannel is unavailable in that scenario, so notifications
cannot supply correctness.

## AC01–AC46 evidence reconciliation

Paths below are relative to their owning repository. References are test names
or specific parametrized scenarios, not claims that mocks prove PostgreSQL.

Backend protected evidence:

- **L**: `tests/integration/test_finance_submissions.py` (Ledger/shared protocol).
- **N**: `tests/integration/test_finance_nested_submissions.py` (Account/Category).
- **T**: `tests/integration/test_finance_transaction_submissions.py` (Income,
  Expense, Internal Transfer).
- **A**: `tests/integration/test_finance_adjustment_submissions.py`.
- **P**: the corresponding `test_finance_*_submission_persistence.py` files,
  plus L's persistence/migration tests.
- **B**: the eleven real browser scenarios above, backed by the independent SQL
  observer in `test_finance_submission_browser.py`.
- **D**: `test_finance_submission_qualification.py` /
  `test_absent_lookup_cannot_cancel_delayed_original_or_allow_duplicate_effects`
  (all five endpoints; all three ordinary Transaction kinds).
- **C**: `tests/test_finance_submission_contract.py` (schema evidence only).

Frontend evidence (browser-controlled protocol responses unless labeled B):

- **J**: `e2e/submission-journal.spec.ts` (T10 shared five-operation matrix).
- **FL/FN/FT/FA**: `e2e/ledger-submissions.spec.ts`,
  `nested-submissions.spec.ts`, `transaction-submissions.spec.ts`,
  `adjustment-submissions.spec.ts` respectively.
- **FC**: colocated `src/components/finance/{ledger,nested,transaction,adjustment}-submissions.test.tsx`
  and `submission-journal.test.tsx` (component/journal evidence only).
- **R**: existing Vitest Finance lifecycle suites and `e2e/smoke.spec.ts`.

| AC   | Concrete evidence and qualification boundary                                                                                                                                                                                                                                                     |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| AC01 | B, all five operations: independent committed rows precede response loss; exact original status/receipt replay, unchanged post-mutation SQL. L/N/T/A also exercise post-completion transport loss.                                                                                               |
| AC02 | L `concurrent_first_admissions_converge_under_submission_key_lock`; N `concurrent_nested_admissions_have_one_effect`; T `concurrent_first_submissions_and_intentional_identical_entries`; A `concurrent_identical_adjustments_converge_on_one_outcome`. Real PostgreSQL serialization.           |
| AC03 | L `bound_content_conflict_never_has_q29_or_rebinds`; N `changed_account_meaning_conflicts_without_touching_position`; T/A canonical meaningful-field matrices.                                                                                                                                   |
| AC04 | L `version_precedence_and_closed_version_keep_original_evidence`; N `common_namespace_version_and_scope_precedence`; T/A `*_guards_never_claim_terminal_or_q29_evidence`.                                                                                                                        |
| AC05 | N `common_namespace_version_and_scope_precedence`; A `adjustment_owner_namespaces_and_scope_nondisclosure`; T guard matrix. Common user/key namespace spans operations/scopes.                                                                                                                   |
| AC06 | L `same_uuid_has_independent_user_namespaces_and_lookup_no_disclosure`; N `different_users_have_independent_nested_submission_namespaces`; A owner/scope matrix.                                                                                                                                 |
| AC07 | L lost-admission-ack test; N `nested_failure_recovery_preserves_atomic_effect_and_receipt`; T/A fault matrices' `admission_ack`: committed unfinished binding survives, explicit original resumes.                                                                                               |
| AC08 | L `failure_after_financial_flush_rolls_back_only_execution` and recognized-rejection rollback; N recognized-rejection rollback; T/A fault matrices (`before_commit`, projection, receipt, partial writes, unknown/recognized failures). SQL counts distinguish rollback from terminal rejection. |
| AC09 | L `lost_commit_acknowledgement_reads_actual_outcome_on_retry`; N/T/A admission/terminal acknowledgement fault matrices call real commit before raising. SQL, not the exception, decides the outcome.                                                                                             |
| AC10 | B rename/archive cases; N `nested_identity_survives_edits_archive_and_resource_deletion`; T `receipt_survives_replacement_archive_and_physical_deletion`; A `created_receipt_survives_adjustment_replacement_and_deletion` (`updated`/`removed`).                                                |
| AC11 | B Income/Transfer/Adjustment physical deletion before original retry; N/T/A retention/deletion tests. Ledger deletion remains outside the domain scope.                                                                                                                                          |
| AC12 | B Adjustment noChange followed by a real `9.99` balance change; A `no_change_is_immutable_after_later_balance_changes_and_archiving`; FA lookup/keyboard/restart scenarios.                                                                                                                      |
| AC13 | B both lost terminal rejections with eligibility restored; L business name rejection; N Category name rejection; T mutable Account rejection; A mutable rejection matrix.                                                                                                                        |
| AC14 | L/N/T/A exact frozen Q29 tests inspect absent binding/effects; J `controlled HTTP evidence exact`; FC strict correlation and durable not-admitted resolution. The browser-controlled echo alone is not admission proof.                                                                          |
| AC15 | J `controlled HTTP evidence generic`; FC generic 422 remains unresolved; L/N generic uncorrelatable validation has no terminal/Q29 evidence.                                                                                                                                                     |
| AC16 | J controlled evidence mismatch variants; FC exact typed Q29/receipt correlation; L/N/T/A delayed-invalid-attempt versus binding races. Mutable business failures produce receipts, never Q29.                                                                                                    |
| AC17 | L `real_lock_contention_is_bounded_and_retry_retains_identity` (`admission`); N `nested_postgresql_waits_are_bounded_and_recoverable`; T/A serialization wait matrices. Actual driver/PostgreSQL lock waits, no sleep-based race.                                                                |
| AC18 | Same protected contention matrices (`execution`/Account locks); J closed/unsupported/error preservation. Explicit retry only; no new automatic POST loop.                                                                                                                                        |
| AC19 | D holds each valid streamed original body behind an event; real lookup returns absent with no effect; releasing the original commits once; original lookup/retry and SQL converge. J `fresh absent lookup` proves the browser retains uncertainty.                                               |
| AC20 | L access-loss/lookup/no-disclosure tests; N/T/A unfinished fault outcomes and owner/scope guards; J fresh inaccessible/failed lookup. Lookup never executes; no command body is disclosed.                                                                                                       |
| AC21 | L/N/T/A rejected receipt lookup is HTTP 200; FC `reload lookup settles terminal rejection, permits acknowledgement and never posts`; FA rejected lookup cases.                                                                                                                                   |
| AC22 | FC receipt/Q29 mismatch and known conflict tests; J `fresh mismatched lookup` and controlled evidence variants. Backend content/version conflicts never disclose an unrelated receipt.                                                                                                           |
| AC23 | L `disabled_user_cannot_disclose_or_execute_known_binding` and access-loss-after-admission; N/T/A disabled-after-admission tests preserve evidence while denying execution/disclosure.                                                                                                           |
| AC24 | J foreign namespace, old-owner delayed callback, inactive-owner recovery-read cases; L/N/T/A owner-header/access guards. Browser partitioning and backend authority have separate evidence.                                                                                                      |
| AC25 | J blocked upgrade/open, incompatible key schema and quota cases; FL/FN/FT/FA aborted preparation; FC preparation failures. Real IndexedDB, no initial POST, no auto-clear.                                                                                                                       |
| AC26 | J legacy prepared-command recovery matrix starts with durably retained unresolved records; fresh lookup cases and FL/FN/FT/FA restart scenarios permit only explicit original POST. This controls the crash boundary; it does not claim a physical power-loss experiment.                        |
| AC27 | FL/FN/FT/FA persistent Chromium profile restart, reload/navigation tests; B real-backend reload preserves serialized original command. No automatic create.                                                                                                                                      |
| AC28 | B Expense two-tab barrier reaches real PostgreSQL twice and observes one effect; J `concurrent retries converge without notifications despite a late failure` and monotonic journal tests cover adversarial local completion ordering.                                                           |
| AC29 | FC Transaction resolve-before-refresh-failure and Adjustment noChange failed-refresh tests; B deleted-resource recovery retains success despite real resource 404. Query refresh failure is not financial rollback.                                                                              |
| AC30 | J `failed resolution and acknowledgement commits retain evidence`; FC resolution-write failure tests subsequently resolve by lookup. Real IndexedDB aborts; backend uniqueness is independently covered by B/P.                                                                                  |
| AC31 | J older read/late acknowledgement/stale absent-record cases; FN concurrent-tab delayed-response tests. Conditional writes cannot resurrect acknowledged evidence or mint a key.                                                                                                                  |
| AC32 | FC exact original retry after draft edits; FL/FN/FT/FA post-dispatch edit/reload matrices. B compares byte-for-byte original request body on real replay.                                                                                                                                        |
| AC33 | B Expense fresh-key identical additional form create, SQL confirms a second economic effect; T intentional-identical Transaction matrix and N identical Account positions. Names remain subject to ordinary Ledger/Category uniqueness.                                                          |
| AC34 | L whitespace/name binding; N Account Money/note/scope matrices; T `canonical_equivalents_match_and_every_meaningful_difference_conflicts`; A canonical equivalents/all immutable fields. Real JSONB binding comparisons.                                                                         |
| AC35 | N signed large Opening Balance; T liability/source-kind matrices; A signed/zero, frozen precision and full durable Money range tests; B exact liability Opening Balance and signed transfer legs. SQL numeric text is compared exactly, with no binary float conversion.                         |
| AC36 | L version precedence/closure and A `closed_v1_keeps_terminal_replay_and_unfinished_resume`; J `closedVersion` preserves pending command/version. This qualifies application admission behavior, not a deployment closure mechanism.                                                              |
| AC37 | N `old_account_q29_rules_do_not_follow_mutable_money_catalog`; T/A frozen Q29/currency/precision tests; J legacy-v1 retry matrix. No old-version validator relaxation.                                                                                                                           |
| AC38 | L header-before-body/admission tests and N/T/A guards: missing headers fail before a binding/financial effect; C required header matrix.                                                                                                                                                         |
| AC39 | Same protected guard matrices cover malformed, unsupported and duplicate/ambiguous headers without fallback, receipt or Q29. J incompatible evidence fails closed.                                                                                                                               |
| AC40 | L immutable evidence/retention constraint test; N retention owner-transfer/rebinding tests; T/A immutable terminal persistence and deletion/disablement cases. Real constraints/triggers prevent evidence loss.                                                                                  |
| AC41 | C `final_five_create_schema_header_status_and_lookup_matrix`, impossible-outcome tests, and generated consumer schemas through frontend check; B original statuses and resource/noChange/rejected outcomes. Schema tests are contract proof, B is runtime proof.                                 |
| AC42 | L `lookup_errors_are_never_cacheable` and cross-user absent equivalence; A scope nondisclosure; B real no-store terminal lookup; J fresh lookup requests.                                                                                                                                        |
| AC43 | L/N/T/A populated migration tests preserve prior resources/evidence without historical backfill. **Operational interrupted-cutover/serving closure/drain proof remains T12/T13**, outside T11; application migration tests cannot establish it.                                                  |
| AC44 | P populated migration/downgrade guards refuse evidence erasure; all five runtime header guards enforce keyed creates. **Actual enforcement-preserving deployment rollback remains T12/T13**; no operational rollback was executed here.                                                          |
| AC45 | Populated migration tests create no fabricated historical submission rows; J corrupt/incompatible legacy evidence is retained without dispatch/rekeying. **Reconciliation of genuinely ambiguous pre-protocol requests remains T12/T13**; T11 does not claim to infer their outcomes.            |
| AC46 | R plus FL/FN/FT/FA keyboard/axe/draft/focus/navigation/replacement/deletion coverage; J storage/lifecycle matrix; B keyboard retry, acknowledgement and settled-render axe checks against real resources. Chromium qualification does not imply all-browser or full WCAG conformance.            |

## Final activation assumptions

The old frontend T02-only integrated test pins a superseded producer and supplies
a controlled unfinished lookup. Its skip must remain explicit; it is not the
completed-protocol proof. B supplies current-pair commit-loss/database evidence.

Application correctness does not establish the serving-boundary create closure,
old-instance/keep-alive drain, outstanding transaction inspection, safe rollback,
or historical ambiguity reconciliation. Those AC43–AC45 operational prerequisites
remain required at T12/T13. Neither successful T11 qualification nor the current
application's rejection of unkeyed requests authorizes activation. No additional
protocol redesign or activation mechanism is introduced by this candidate.
