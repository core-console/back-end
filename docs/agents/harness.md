# Repository agent harness

The repository-local harness records deterministic Git/worktree state and validation
evidence. It supports review; it does not automate Standards or Spec judgement.

## Candidate lifecycle

Use the local `implement-candidate` skill to produce one uncommitted candidate. After
protected harness validation, generate `review-state`; its artifact is the canonical
input for a fresh independent review session. Approval comes after that review and
before commit.

Committing changes the snapshot identity, even when the worktree bytes are unchanged.
Before publication, generate fresh protected validation and review-state evidence for
the committed snapshot. Uncommitted receipts are neither promoted nor treated as
content-equivalent. The publication harness then owns push, exact-SHA CI verification,
and implementation-issue closure.

Run commands from the repository root with an explicit review base:

```powershell
uv run --frozen python scripts/agent_harness.py preflight --base <sha>
uv run --frozen python scripts/agent_harness.py validate --base <sha> --protected
uv run --frozen python scripts/agent_harness.py review-state --base <sha>
```

Preflight reads the existing local `origin/main` ref and never fetches. Full protected
validation preserves the checks and order owned by `scripts/validate.py`, then runs
resolved base-to-HEAD, unstaged, and staged `git diff --check`. It requires
`TEST_DATABASE_URL`, forces the existing PostgreSQL fixtures to fail rather than skip,
and reports success only after pytest actually passes in that mode. It never falls back
to `DATABASE_URL`.

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

Versioned JSON receipts are written under `.agent/receipts/`; detailed validation logs
are under `.agent/logs/<snapshot-digest>/`; canonical review artifacts are under
`.agent/reviews/`. Review artifacts contain committed, staged, and unstaged binary-
capable full-index patches plus complete untracked UTF-8 content. Untracked binary or
unusual content is identified by path, byte count, SHA-256, and lossless base64.
Failed validation prints only the failed check and receipt/log locations; detailed
output is read from the stored log on demand.

A validation PASS is valid only when its before/after snapshot digests match. Review
evidence is assembled between two implementation snapshots; a digest change aborts
generation without a READY artifact or receipt. Review schema
`core-console-agent-review/v2` reports validation snapshot identity (`matching`,
`stale`, or `absent`) separately from outcome, protected PostgreSQL requested/exercised
state, and receipt path. Snapshot identity matching alone is not validation approval.

A reviewer may trust a matching PASS receipt only when protected PostgreSQL was both
requested and exercised, and normally run only targeted tests relevant to concrete
findings. Rerun the full protected gate when the receipt is absent or stale, the outcome
is not PASS, PostgreSQL was not requested or exercised, harness or validation
infrastructure changed, dependencies or configuration changed materially, database
protection/migrations/CI behavior changed, or environment-sensitive behavior is under
review. A receipt never replaces Standards and Spec reasoning.

## Explicit publication

Publication is a separate, explicitly invoked operation. Validation and review receipts
are evidence only; they never authorize a commit, push, CI rerun, or issue closure.

```powershell
uv run --frozen python scripts/agent_harness.py publish `
  --issue <number> --base <parent-sha> --sha <approved-sha> --branch <branch>
```

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

Only after exact-SHA CI succeeds does the harness recheck the live remote SHA, inspect
exactly the supplied issue, and, when it is OPEN, recheck the live remote again as the
final operation immediately before closing it with reason `completed`. An
already-pushed SHA and an already-closed supplied issue are verified as completed
steps, so reruns can resume after interruption without another commit, a different
push, or duplicate closure. Closure ambiguity is resolved by rerunning and reading the
issue state.

Versioned success or exact-SHA CI failure receipts are written to
`.agent/receipts/publication-issue-<number>-<approved-sha>.json`; verbose GitHub command
responses remain under `.agent/logs/publication/`. Normal stdout contains only compact
identifiers and receipt pointers. Publication-specific remote Git commands also retain
complete stdout/stderr there; failures report only the phase and detailed-log path.
