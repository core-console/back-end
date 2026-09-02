# Repository agent harness

The repository-local harness records deterministic Git/worktree state and validation
evidence. It supports review; it does not automate Standards or Spec judgement.

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
