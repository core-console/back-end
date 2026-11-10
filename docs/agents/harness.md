# Repository agent harness

The repository-local harness records deterministic Git/worktree state and validation
evidence. It supports review; it does not automate Standards or Spec judgement.

## Candidate lifecycle

Use the installed Matt `implement` skill for the approved spec or tickets, with these
repository overrides:

1. Use Matt's TDD loop where possible at agreed seams, with targeted tests and
   typechecking during implementation. Use protected harness validation below as
   the final complete suite, rather than running a second standalone full suite.
2. After protected validation, generate `review-state`. Use Matt `code-review`'s
   parallel Standards and Spec sub-agents against that canonical artifact, or reuse
   their existing reports when they meet the review evidence policy below. Supply
   the approved user spec or ticket and repository standards to the reviewers.
   Review all committed, staged, unstaged, and untracked candidate content; Matt's
   default `git diff <fixed-point>...HEAD` alone omits uncommitted work and is not the
   review input here. Resolve findings and regenerate evidence after candidate changes.
3. Stop uncommitted at `READY_FOR_REVIEW` instead of Matt `implement`'s default commit
   step. Keep the index untouched when the user requests an unstaged candidate.

Matt skills own implementation and independent Standards/Spec judgement. The harness
owns snapshot identity, protected validation, canonical review evidence, and separately
authorized publication. A READY review-state artifact is preparation evidence, not a
Standards or Spec approval. Valid independent Matt reviews satisfy the review gate in
the implementation session; a separate fresh-session review is optional. User approval
is still required after review and before commit or publication. Installed skills
remain unchanged; this repository needs no separate candidate implementation skill.

Committing changes the snapshot identity, even when the worktree bytes are unchanged.
Before publication, generate fresh protected validation and review-state evidence for
the committed snapshot. Uncommitted receipts are neither promoted nor treated as
content-equivalent. The publication harness then owns push, exact-SHA CI verification,
and closure of an explicitly supplied implementation issue. Issue linkage is optional.

Run commands from the repository root with an explicit review base:

```powershell
uv run --frozen python scripts/agent_harness.py preflight --base <sha>
uv run --frozen python scripts/agent_harness.py validate --base <sha> --protected
uv run --frozen python scripts/agent_harness.py review-state --base <sha>
```

Preflight reads the existing local `origin/main` ref and never fetches. Full protected
validation preserves the checks and order owned by `scripts/validate.py`, then runs
resolved base-to-HEAD, unstaged, and staged `git diff --check`. It requires
`TEST_DATABASE_URL`, forces the existing PostgreSQL fixtures to fail when it is
missing, and never falls back to `DATABASE_URL`.

The protected pytest child uses isolated Python, the repository's `pyproject.toml`,
a fixed full-suite argument list, and an explicit AnyIO plugin. It rejects
`PYTEST_ADDOPTS` and `PYTEST_PLUGINS`, disables plugin autoload, and overrides
configuration `addopts` with the checked-in protected profile. Use ordinary pytest
for ad hoc options and selections. A protected PASS requires successful session
completion, test-body entry and complete setup/call/teardown evidence for the selected collection,
zero deselections or failures, and a passing test whose call issued SQL against
the guarded PostgreSQL target. Legitimate skips are counted separately, including
items skipped during setup that never entered the test body.

### Safe diagnostics

Pytest scrubs diagnostic values before warning formatting, truncation, or wrapping. It
also scrubs failure representations, report identifiers, captured output, and JUnit
metadata before the standard reporters persist or display them. It includes the
invocation directory's dotenv file when pytest selects a different root. The local/CI
validation entry point and the Harness sanitize subprocess output before displaying
or saving it.
Redaction includes database URLs from the environment and local dotenv file, their
credential components, and encoded forms. The same redactor checks nested Python and
JSON representations and captured subprocess text in UTF-8, the platform encoding,
and any configured `PYTHONIOENCODING`. Receipts record safe database identifiers
and structured outcomes. Gate log hashes cover the sanitized bytes written to disk.

Use captured pytest reporting for diagnostic runs. An explicitly uncaptured shell,
debugger, or custom reporter that bypasses the report hooks can expose raw input.

### Disposable database ownership

The fixture checks the URL and development-target separation before opening the
owner connection, then verifies the actual database identity before attempting a
nonblocking, database-local PostgreSQL advisory lease. Routing and session
overrides that would bypass those checks are rejected. The same owner session
resets the public schema and runs Alembic in a transaction. Evidence includes the
database identity, server version, public schema OIDs before and after reset, and
the expected Alembic heads read from the live database. The owner verifies the heads
again and releases its lease after all dependent fixture cleanup.

The Harness enrolls SQLAlchemy pools on the guarded Psycopg route. Before handing a
physical session to application SQL, pool checkout verifies its database identity on first use,
acquires the shared advisory lock once per session, and checks that both the owner
and client locks are held. Normal reuse verifies the locks without acquiring another
one. Invalidation and reconnection go through checkout even when SQLAlchemy keeps the
same connection wrapper. A checkout that cannot establish protection fails closed.

The shared lock survives rollback and checkin. Closing a physical session releases
its lock, including when a pool disposes idle sessions or an invalidated session is
discarded. A replacement run needs the exclusive form of the shared key before reset,
so surviving clients from a lost owner still exclude a reset. A busy target or an
incomplete lifecycle fails the gate. Use direct PostgreSQL connections with session
semantics. Clients outside the guarded Harness route can bypass advisory coordination.

Schedule processes sharing a target serially and provision distinct guarded targets
for independent parallel jobs. The lease coordinates the repository integration
fixture, including direct pytest and CI runs. Other tools that mutate the target
must be scheduled separately. Tests may still exercise concurrency within a run.

The kill/recovery, synchronous revalidation, migration rollback, and wrong-target probes reset
the same disposable database. Run them as a separate, serial command:

```powershell
$env:CORE_CONSOLE_TEST_POSTGRES_RECOVERY = "1"
uv run --frozen python -m pytest tests/integration/test_postgres_ownership.py -k "not concurrent_reset and not async_revalidation"
Remove-Item Env:CORE_CONSOLE_TEST_POSTGRES_RECOVERY
```

The normal suite runs the contention and asynchronous revalidation probes while its own
fixture holds the lease.

## Stage a fresh Finance compatibility campaign

Use `tests/browser/stage-finance-compatibility.mjs` with Node.js 24 for every
fresh compatibility campaign. Pass the seed directory, a new output directory,
and the exact producer and consumer commits for both the seed and the target.
The seed must contain `acceptance.py`, `run.py`, `database.py`, and an unpatched
`browser.mjs`. Each of the three pair-bound files must contain the seed's exact
producer and consumer SHAs once. For the existing launcher, use a direct child
of the backend's `.agent/` directory so the launcher resolves both repositories.
The output directory must not already exist and its parent must exist.

```powershell
node tests/browser/stage-finance-compatibility.mjs $seed $fresh `
  --from-producer $seedProducer --from-consumer $seedConsumer `
  --producer $producer --consumer $consumer
```

The command rebinds the pair in `acceptance.py`, `run.py`, and `browser.mjs`,
preserves the seed's database helper, and applies the route drain before writing
the `browser.mjs` that `run.py` launches. Use the staged `run.py` only after
staging succeeds and the campaign's separate setup and qualification gates pass.
The command stages source files only. It leaves the seed, historical evidence,
and campaign results alone and does not start services or access a database.

Run the database-free staging regression with
`node --test tests/browser/stage-finance-compatibility.test.mjs`.

## Snapshot digest

Snapshot schema `core-console-agent-snapshot/v1` is SHA-256 over ordered,
eight-byte-big-endian length-prefixed `(label, value)` records. Records contain:

1. schema, resolved base commit SHA, and HEAD SHA;
2. the raw full-object-ID Git diff from base to HEAD;
3. the complete Git index (`git ls-files --stage -z`);
4. raw full-object-ID staged and unstaged Git diffs, including tracked deletion and
   mode information;
5. every current/HEAD-tracked path in lexical order and its worktree bytes, or an
   explicit missing marker; and
6. every non-ignored untracked path in lexical order and its bytes.

Paths come from NUL-delimited Git output. Timestamps and other unstable filesystem
metadata are excluded. `.agent/` is the dedicated ignored output area and is excluded
from snapshot identity, so writing receipts or logs cannot invalidate the snapshot.
Any implementation path or content change affects at least one digest record.

## Receipts and review policy

Each validation attempt reserves a new execution ID under `.agent/validation/<id>/`
and atomically writes a start record before running any gate. It retains the fixed ordered
command configuration, purpose, profile, repository, base, HEAD, and snapshot
digest. The pytest observer binds its phase reports and PostgreSQL lifecycle
evidence to the hash of that start record. Logs and the observer report stay
beside it. A completed v2 receipt is sealed atomically without replacement at
`.agent/receipts/validation-<snapshot-digest>-<id>.json`. It records timestamps,
outcome, counts, tool versions from the test invocation, and hashes for the
observer report and every retained gate log. A forced stop can leave a start
record and partial logs. A caught interruption seals a failure with interrupted
status. Neither is eligible for PASS. Sealing requires a filesystem that supports
hard links within `.agent/`.

By default, `review-state` selects the newest started current protected execution
for the snapshot and reports failed or incomplete attempts without falling back
to an older PASS. Supply `--validation-execution <id>` to select an exact attempt.
Use `validate --protected --purpose recovery` for fresh recovery evidence. It has
the same execution gates and requires explicit selection in review-state.
Historical v1 delivery evidence retains its original filename, bytes, and meaning.
The reader labels matching v1 evidence `historical-v1`. Publication requires a
current, canonical, protected v2 PASS for the committed snapshot. Recovery and
historical receipts remain evidence for their original purposes.

Review artifacts under `.agent/reviews/` contain committed, staged, and unstaged
binary-capable full-index patches plus complete untracked UTF-8 content. Untracked
binary or unusual content includes the path, byte count, SHA-256, and lossless
base64. Review v3 uses separate artifact and receipt paths from historical review
files. The review-state receipt is the current selection for its snapshot. It
binds the artifact hash and the chosen execution ID, receipt path, and receipt
hash. Publication rechecks that exact execution's receipt, logs, and pytest proof
before proceeding. These local hashes detect accidental changes and substitutions
relative to the recorded binding. They do not authenticate a writer with access to
all evidence files.

A validation PASS requires matching before and after snapshots. Review evidence
is also assembled between two implementation snapshots. Review schema
`core-console-agent-review/v3` reports snapshot identity independently of outcome,
protected PostgreSQL proof, and evidence class. Matching snapshot identity alone
does not qualify a receipt. A reviewer can rely on a current protected PASS
covering the exact candidate and run targeted tests for concrete findings. Rerun
the protected gate after candidate changes or when the evidence is absent,
failed, stale, incomplete, or from a different profile.

### Standards and Spec evidence

Retain both sub-agent reports in the conversation or a durable local file under
`.agent/reviews/`, with a reference to each report in the candidate handoff. The harness
does not generate or verify these judgements. Each axis must record:

- its independent reviewer identity (sub-agent/session reference) and axis;
- the resolved base SHA, HEAD SHA, snapshot digest, complete changed-file inventory,
  canonical artifact path, and validation/review receipt paths it reviewed;
- its sources: repository standards for Standards, and the approved user request or
  ticket/spec for Spec;
- its verdict, findings, and disposition of every finding. Blocking findings must be
  resolved and reviewed on the final candidate; retain any non-blocking notes.

Existing reports qualify only when both axes cover the complete current candidate,
their sources still describe the approved scope and applicable standards, and the
current receipt shows matching protected validation PASS with PostgreSQL requested
and exercised. A skipped Spec review, self-review by the implementer, empty fixed-point
diff, summary claiming PASS without the reports, or READY artifact alone is insufficient.

Before handoff or relying on previous reviews for approval, rerun `review-state` with
the pinned base and compare its identity and inventory with both reports. Reuse them
only if these still match. Candidate changes, including staging or a changed base/HEAD,
invalidate the old review binding. Regenerate protected validation and the artifact,
then obtain replacement Standards and Spec reviews of the complete candidate. Do the
same when scope or standards changes make the earlier review insufficient.

When reports are missing or incomplete, obtain the missing or replacement independent
axis reviews against the current canonical artifact. If validation is absent, stale,
failed, or unprotected, first rerun the protected gate and regenerate `review-state`.
A fresh session can perform this recovery or an additional review at the user's request;
opening a new session alone never establishes coverage. Stop before handoff as
`READY_FOR_REVIEW` or requesting delivery approval until both axes and validation qualify.
The handoff includes the pinned candidate, evidence/report references, and separate
Standards and Spec outcomes. Evidence reuse never supplies delivery authorization.

## Explicit publication

Publication is a separate, explicitly invoked operation. Validation and review receipts
are evidence only; they never authorize a commit, push, CI rerun, or issue closure.

```powershell
uv run --frozen python scripts/agent_harness.py publish `
  --base <parent-sha> --sha <approved-sha> --branch <branch>
```

Add `--issue <number>` only when publication includes that issue. Without it, the
harness neither looks up nor closes an issue, and publication needs no invented tracker
item. Both paths require the same approved candidate and all validation, review,
destination, push, exact-SHA CI/job, final remote equality, and receipt evidence.

Issue-specific cross-repository gates also remain required. Backend publication alone
does not satisfy a ticket that requires delivered producer and consumer revisions and
pair verification. Omit `--issue` until those requirements and explicit closure
authorization are satisfied; the publication harness does not infer consumer readiness.

The approved SHA must be the current HEAD and the single child of the resolved base.
The expected branch must be checked out, the worktree must be clean, local
`origin/<branch>` must describe exactly the unpublished or already-pushed state, and
the approved snapshot must have matching validation and review-state receipts. The
validation must be an unchanged protected PASS with PostgreSQL both requested and
exercised. No stale or merely filename-matching receipt is accepted.

An explicit-URL publication does not refresh local `origin/<branch>`. If that tracking
ref is stale before the next committed-snapshot publication, an explicitly authorized
normal fetch may refresh it. Run preflight afterward and verify the refreshed tracking
ref and live remote still identify the expected base before continuing. The harness
never fetches implicitly, and a mismatch stops publication; do not reset, rebase,
force, or use another destructive repair to manufacture the expected state.

Immediately before a first push, the harness reads `refs/heads/<branch>` with
`git ls-remote`, which does not update local refs. Each read has a subprocess timeout;
a small attempt limit and one overall deadline bound the whole read. Only recognized
transient transport failures are retried, with a separate retained transcript for each
attempt. Authentication and certificate diagnostics take precedence even when the
subprocess also reports a timeout; they fail immediately. Malformed evidence,
repository binding, SHA mismatch, and other semantic safety failures also stop without
retry. Push is attempted once and is never retried. Remote drift stops publication.
The origin fetch URL and all configured push URLs are resolved first. Git then resolves
the effective push URL, including `url.*.insteadOf` and `url.*.pushInsteadOf`
rewriting.
Publication requires exactly one effective strict GitHub push destination with the same
repository identity as the fetch URL, and the normal fast-forward push names that
verified effective URL directly. Before publication, Git resolves that URL once more as
a destination with `ls-remote --get-url`; any further `insteadOf` rewrite or ambiguity
fails closed. Because Git has no equivalent no-contact proof for an explicit URL under
remaining `pushInsteadOf` rules, their presence also fails closed:
`git push <verified-url> <approved-sha>:refs/heads/<branch>`. Ambiguous, unparsable, or
mismatched destinations stop before push. The harness never automatically forces,
merges, rebases, resets, amends, squashes, or otherwise rewrites history.

After push, the harness considers only workflow runs named `Validate` whose `headSha`
equals the approved SHA. If several exist, the greatest numeric run ID wins. It waits
for that exact run ID to complete successfully and requires the repository's complete
job inventory: one terminal, successful `validate` job. Missing, malformed,
non-terminal, inconsistent, or unsuccessful run/job evidence stops before issue lookup.
The remaining CI deadline bounds every GitHub run-list and run-view subprocess; a
command timeout follows the same compact failure path. Failure, cancellation, timeout,
or unreliable identification stops without changing code, rerunning CI, creating
commits, or closing the issue.
Every GitHub CLI operation passes the repository identity parsed from `origin`
explicitly, so ambient `gh` configuration cannot redirect CI lookup or issue closure.

Only after exact-SHA CI succeeds does the harness recheck the live remote SHA. When
an issue was explicitly supplied, it then inspects exactly that issue and, when it is
OPEN, rechecks the live remote again as the final operation immediately before closing
it with reason `completed`. An already-pushed SHA and an already-closed supplied issue
are verified as completed
steps, so reruns can resume after interruption without another commit, a different
push, or duplicate closure. Closure ambiguity is resolved by rerunning and reading the
issue state. Every successful publication, with or without an issue, still requires
the final clean-worktree, local HEAD, and live remote equality check before its PASS
receipt.

Versioned success or exact-SHA CI failure receipts are written to
`.agent/receipts/publication-issue-<number>-<approved-sha>.json`; verbose GitHub command
responses remain under `.agent/logs/publication/issue-<number>-<approved-sha>/`.
Issue-free receipts use `.agent/receipts/publication-no-issue-<approved-sha>.json` and
logs use `.agent/logs/publication/no-issue-<approved-sha>/`. Their `issueNumber` and
`finalIssueState` fields are null, including on exact-SHA CI failure; supplied-issue
receipt fields and paths retain their existing meaning. Normal stdout contains only
compact identifiers and receipt pointers. Publication-specific remote Git commands also retain
complete stdout/stderr there; failures report only the phase and detailed-log path.
