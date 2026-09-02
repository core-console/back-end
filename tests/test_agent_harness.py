"""Behavior tests for the repository-local agent harness."""

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.agent_harness as agent_harness
from scripts.agent_harness import (
    ReviewSnapshotStaleError,
    ValidationCheck,
    collect_preflight,
    create_review_state,
    format_validation_result,
    snapshot_repository,
    validate_repository,
)


def git(repository: Path, *arguments: str) -> str:
    """Run Git in an isolated test repository."""

    result = subprocess.run(
        ("git", *arguments),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def initialized_repository(tmp_path: Path) -> tuple[Path, str]:
    """Create a repository with one committed implementation file."""

    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "--initial-branch=main")
    git(repository, "config", "user.name", "Harness Test")
    git(repository, "config", "user.email", "harness@example.test")
    (repository / ".gitignore").write_text(".agent/\n", encoding="utf-8")
    (repository / "implementation.txt").write_text("initial\n", encoding="utf-8")
    git(repository, "add", ".gitignore", "implementation.txt")
    git(repository, "commit", "-m", "test: establish fixture")
    return repository, git(repository, "rev-parse", "HEAD")


def test_identical_snapshot_has_identical_digest(tmp_path: Path) -> None:
    """Unchanged repository state has stable semantic identity."""

    repository, base_sha = initialized_repository(tmp_path)

    first = snapshot_repository(repository, base_sha)
    second = snapshot_repository(repository, base_sha)

    assert first.digest == second.digest
    assert first.base_sha == base_sha
    assert first.head_sha == base_sha


def test_staged_modification_changes_digest(tmp_path: Path) -> None:
    """The index is part of snapshot identity."""

    repository, base_sha = initialized_repository(tmp_path)
    implementation = repository / "implementation.txt"
    implementation.write_text("same worktree bytes\n", encoding="utf-8")
    while_unstaged = snapshot_repository(repository, base_sha)
    git(repository, "add", "implementation.txt")
    after_staging_same_bytes = snapshot_repository(repository, base_sha)

    assert implementation.read_text(encoding="utf-8") == "same worktree bytes\n"
    assert after_staging_same_bytes.digest != while_unstaged.digest


def test_unstaged_modification_changes_digest(tmp_path: Path) -> None:
    """Tracked worktree content is part of snapshot identity."""

    repository, base_sha = initialized_repository(tmp_path)
    before = snapshot_repository(repository, base_sha)
    (repository / "implementation.txt").write_text("unstaged\n", encoding="utf-8")

    assert snapshot_repository(repository, base_sha).digest != before.digest


def test_untracked_file_addition_changes_digest(tmp_path: Path) -> None:
    """Adding untracked implementation content changes snapshot identity."""

    repository, base_sha = initialized_repository(tmp_path)
    before = snapshot_repository(repository, base_sha)
    untracked = repository / "new.txt"
    untracked.write_text("fixed bytes\n", encoding="utf-8")
    with_file = snapshot_repository(repository, base_sha)

    assert with_file.digest != before.digest


def test_untracked_path_changes_digest_with_identical_content(tmp_path: Path) -> None:
    """Untracked filename identity is independent from file content identity."""

    repository, base_sha = initialized_repository(tmp_path)
    first_path = repository / "a.txt"
    second_path = repository / "b.txt"
    first_path.write_text("fixed bytes\n", encoding="utf-8")
    first_path_digest = snapshot_repository(repository, base_sha)
    first_path.rename(second_path)
    second_path_digest = snapshot_repository(repository, base_sha)

    assert second_path.read_text(encoding="utf-8") == "fixed bytes\n"
    assert second_path_digest.digest != first_path_digest.digest


def test_untracked_content_changes_digest_at_same_path(tmp_path: Path) -> None:
    """Untracked content bytes are independently part of snapshot identity."""

    repository, base_sha = initialized_repository(tmp_path)
    untracked = repository / "new.txt"
    untracked.write_text("first\n", encoding="utf-8")
    first_content = snapshot_repository(repository, base_sha)
    untracked.write_text("second\n", encoding="utf-8")
    with_changed_content = snapshot_repository(repository, base_sha)

    assert untracked.name == "new.txt"
    assert with_changed_content.digest != first_content.digest


def test_tracked_deletion_changes_digest(tmp_path: Path) -> None:
    """Missing tracked content remains visible in the snapshot."""

    repository, base_sha = initialized_repository(tmp_path)
    before = snapshot_repository(repository, base_sha)
    (repository / "implementation.txt").unlink()

    assert snapshot_repository(repository, base_sha).digest != before.digest


def test_harness_output_does_not_change_digest(tmp_path: Path) -> None:
    """Ignored harness receipts and logs are outside implementation identity."""

    repository, base_sha = initialized_repository(tmp_path)
    before = snapshot_repository(repository, base_sha)
    output = repository / ".agent" / "logs" / "validation.log"
    output.parent.mkdir(parents=True)
    output.write_text("receipt output\n", encoding="utf-8")

    assert snapshot_repository(repository, base_sha).digest == before.digest


def test_review_state_contains_staged_unstaged_and_untracked_work(
    tmp_path: Path,
) -> None:
    """Review evidence classifies every uncommitted implementation layer."""

    repository, base_sha = initialized_repository(tmp_path)
    implementation = repository / "implementation.txt"
    implementation.write_text("staged\n", encoding="utf-8")
    git(repository, "add", "implementation.txt")
    implementation.write_text("staged and unstaged\n", encoding="utf-8")
    (repository / "new.txt").write_text("review me\n", encoding="utf-8")

    review = create_review_state(repository, base_sha)
    files = {item.path: item for item in review.changed_files}
    artifact = json.loads(review.artifact_path.read_text(encoding="utf-8"))

    assert files["implementation.txt"].staged is True
    assert files["implementation.txt"].unstaged is True
    assert files["implementation.txt"].untracked is False
    assert files["new.txt"].untracked is True
    assert "+staged\n" in artifact["stagedPatch"]
    assert "+staged and unstaged\n" in artifact["unstagedPatch"]
    assert artifact["untrackedFiles"][0]["path"] == "new.txt"
    assert artifact["untrackedFiles"][0]["content"] == (repository / "new.txt").read_bytes().decode(
        "utf-8"
    )


def test_review_artifact_contains_untracked_text_content(tmp_path: Path) -> None:
    """A reviewer can read new text files without committing them first."""

    repository, base_sha = initialized_repository(tmp_path)
    (repository / "new.txt").write_text("complete untracked content\n", encoding="utf-8")

    review = create_review_state(repository, base_sha)
    artifact = review.artifact_path.read_text(encoding="utf-8")

    assert "complete untracked content" in artifact


def test_review_state_rejects_worktree_mutation_during_evidence_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review READY evidence is emitted only for a coherent implementation snapshot."""

    repository, base_sha = initialized_repository(tmp_path)
    starting_snapshot = snapshot_repository(repository, base_sha)
    evidence_collection_started = threading.Event()
    allow_collection_to_continue = threading.Event()
    original_patch = agent_harness._patch

    def synchronized_patch(root: Path, *arguments: str) -> str:
        if not evidence_collection_started.is_set():
            evidence_collection_started.set()
            assert allow_collection_to_continue.wait(timeout=5)
        return original_patch(root, *arguments)

    monkeypatch.setattr(agent_harness, "_patch", synchronized_patch)

    with ThreadPoolExecutor(max_workers=1) as executor:
        review = executor.submit(create_review_state, repository, base_sha)
        assert evidence_collection_started.wait(timeout=5)
        (repository / "implementation.txt").write_text("concurrent change\n", encoding="utf-8")
        allow_collection_to_continue.set()

        with pytest.raises(ReviewSnapshotStaleError, match="changed while review evidence"):
            review.result(timeout=5)

    output = repository / ".agent"
    assert not (output / "reviews" / f"{starting_snapshot.digest}.json").exists()
    assert not (output / "receipts" / f"review-{starting_snapshot.digest}.json").exists()


def test_validation_receipt_matches_only_the_validated_snapshot(tmp_path: Path) -> None:
    """Review state rejects formerly valid evidence after implementation drift."""

    repository, base_sha = initialized_repository(tmp_path)
    checks = (ValidationCheck("test check", (sys.executable, "-c", "print('1 passed')")),)
    validation = validate_repository(repository, base_sha, protected=False, checks=checks)

    assert validation.outcome == "PASS"
    assert create_review_state(repository, base_sha).validation_status == "matching"

    (repository / "implementation.txt").write_text("changed later\n", encoding="utf-8")

    assert create_review_state(repository, base_sha).validation_status == "stale"


def test_review_rejects_mismatched_receipt_content_even_when_filename_matches(
    tmp_path: Path,
) -> None:
    """Receipt binding verifies structured identity instead of trusting its filename."""

    repository, base_sha = initialized_repository(tmp_path)
    checks = (ValidationCheck("test check", (sys.executable, "-c", "print('1 passed')")),)
    validation = validate_repository(repository, base_sha, protected=False, checks=checks)
    receipt = json.loads(validation.receipt_path.read_text(encoding="utf-8"))
    receipt["snapshotDigest"] = "0" * 64
    validation.receipt_path.write_text(
        f"{json.dumps(receipt, indent=2, sort_keys=True)}\n",
        encoding="utf-8",
    )

    assert create_review_state(repository, base_sha).validation_status == "stale"


@pytest.mark.parametrize(
    ("return_code", "protected", "expected_outcome", "requested", "exercised"),
    (
        pytest.param(1, False, "FAIL", False, False, id="matching-fail"),
        pytest.param(0, False, "PASS", False, False, id="matching-unprotected-pass"),
        pytest.param(0, True, "PASS", True, True, id="matching-protected-pass"),
    ),
)
def test_review_cli_distinguishes_validation_identity_outcome_and_protection(
    tmp_path: Path,
    return_code: int,
    protected: bool,
    expected_outcome: str,
    requested: bool,
    exercised: bool,
) -> None:
    """Snapshot matching never hides validation failure or missing PostgreSQL proof."""

    repository, base_sha = initialized_repository(tmp_path)
    environment = os.environ.copy()
    if protected:
        environment["TEST_DATABASE_URL"] = "postgresql+psycopg://test.invalid/core_test"
    check = ValidationCheck(
        "pytest",
        (sys.executable, "-c", f"raise SystemExit({return_code})"),
    )
    validate_repository(
        repository,
        base_sha,
        protected=protected,
        checks=(check,),
        environment=environment,
    )
    script = Path(__file__).resolve().parents[1] / "scripts" / "agent_harness.py"

    completed = subprocess.run(
        (
            sys.executable,
            str(script),
            "--repo",
            str(repository),
            "review-state",
            "--base",
            base_sha,
        ),
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )
    output = json.loads(completed.stdout)

    assert completed.returncode == 0
    assert output["validation"] == {
        "snapshotStatus": "matching",
        "outcome": expected_outcome,
        "protectedPostgresql": {
            "requested": requested,
            "exercised": exercised,
        },
        "receiptPath": f".agent/receipts/validation-{output['snapshotDigest']}.json",
    }


def test_full_protected_validation_fails_without_test_database(
    tmp_path: Path,
) -> None:
    """Protected mode cannot degrade into skipped PostgreSQL coverage."""

    repository, base_sha = initialized_repository(tmp_path)
    environment = os.environ.copy()
    environment.pop("TEST_DATABASE_URL", None)

    result = validate_repository(
        repository,
        base_sha,
        protected=True,
        checks=(),
        environment=environment,
    )

    assert result.outcome == "FAIL"
    assert result.failed_check == "protected PostgreSQL preflight"
    assert result.protected_postgresql_exercised is False


def test_compact_success_output_omits_complete_check_logs(tmp_path: Path) -> None:
    """Successful command output keeps verbose tool output in its log file."""

    repository, base_sha = initialized_repository(tmp_path)
    marker = "verbose-success-detail-that-must-stay-in-log"
    checks = (
        ValidationCheck(
            "pytest",
            (sys.executable, "-c", f"print('{marker}'); print('7 passed')"),
        ),
    )

    result = validate_repository(repository, base_sha, protected=False, checks=checks)
    output = format_validation_result(result)

    assert result.outcome == "PASS"
    assert marker not in output
    assert "7 passed" in output
    assert marker in result.checks[0].log_path.read_text(encoding="utf-8")


def test_failure_cli_reports_log_path_without_emitting_complete_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Failure output stays compact until a reviewer opens the detailed log."""

    repository, base_sha = initialized_repository(tmp_path)
    start_marker = "distinctive-failure-log-start"
    end_marker = "distinctive-failure-log-end"
    check = ValidationCheck(
        "large failing check",
        (
            sys.executable,
            "-c",
            (
                "import sys; "
                f"sys.stderr.write('{start_marker}\\n' + 'detail\\n' * 5000 + "
                f"'{end_marker}\\n'); raise SystemExit(9)"
            ),
        ),
    )
    result = validate_repository(
        repository,
        base_sha,
        protected=False,
        checks=(check,),
    )
    monkeypatch.setattr(agent_harness, "validate_repository", lambda *_args, **_kwargs: result)

    exit_code = agent_harness.main(("--repo", str(repository), "validate", "--base", base_sha))
    captured = capsys.readouterr()
    combined_output = f"{captured.out}\n{captured.err}"

    assert exit_code == 1
    assert "FAIL check=large failing check" in captured.out
    assert str(result.receipt_path) in combined_output
    assert f"detailed-log={result.checks[0].log_path}" in captured.err
    assert start_marker not in combined_output
    assert end_marker not in combined_output
    stored_log = result.checks[0].log_path.read_text(encoding="utf-8")
    assert start_marker in stored_log
    assert end_marker in stored_log


def test_validation_becomes_stale_if_a_check_changes_implementation(
    tmp_path: Path,
) -> None:
    """A successful tool cannot certify a different post-validation snapshot."""

    repository, base_sha = initialized_repository(tmp_path)
    mutating_check = ValidationCheck(
        "mutating check",
        (
            sys.executable,
            "-c",
            "from pathlib import Path; Path('implementation.txt').write_text('drift\\n')",
        ),
    )

    result = validate_repository(
        repository,
        base_sha,
        protected=False,
        checks=(mutating_check,),
    )

    assert result.outcome == "STALE"
    assert result.failed_check == "implementation snapshot changed during validation"


def test_validation_checks_committed_whitespace_after_resolved_base(tmp_path: Path) -> None:
    """A clean worktree cannot hide whitespace defects committed after the review base."""

    repository, base_sha = initialized_repository(tmp_path)
    (repository / "implementation.txt").write_text("committed trailing space \n", encoding="utf-8")
    git(repository, "add", "implementation.txt")
    git(repository, "commit", "-m", "test: add committed whitespace defect")

    assert subprocess.run(("git", "diff", "--check"), cwd=repository, check=False).returncode == 0
    assert (
        subprocess.run(
            ("git", "diff", "--cached", "--check"), cwd=repository, check=False
        ).returncode
        == 0
    )

    result = validate_repository(
        repository,
        base_sha,
        protected=False,
        checks=(),
    )

    assert result.outcome == "FAIL"
    assert result.failed_check == "base to HEAD git diff --check"
    failed = result.checks[-1]
    assert failed.label == "base to HEAD git diff --check"
    assert "trailing whitespace" in failed.log_path.read_text(encoding="utf-8")


def test_preflight_reports_local_remote_tracking_state_without_fetching(
    tmp_path: Path,
) -> None:
    """Preflight identifies the local repository and origin/main relationship."""

    repository, base_sha = initialized_repository(tmp_path)
    git(repository, "remote", "add", "origin", "https://example.test/core-console/back-end.git")
    git(repository, "update-ref", "refs/remotes/origin/main", base_sha)
    (repository / "new.txt").write_text("dirty\n", encoding="utf-8")

    preflight = collect_preflight(repository, base_sha)

    assert preflight["headSha"] == base_sha
    assert preflight["localOriginMainSha"] == base_sha
    assert preflight["ahead"] == 0
    assert preflight["behind"] == 0
    assert preflight["dirty"] is True
    assert preflight["remoteTrackingState"] == "local; no implicit fetch"


def test_direct_cli_can_load_existing_validation_authority(tmp_path: Path) -> None:
    """Direct script execution reaches a structured gate result, not an import crash."""

    repository, base_sha = initialized_repository(tmp_path)
    script = Path(__file__).resolve().parents[1] / "scripts" / "agent_harness.py"

    completed = subprocess.run(
        (
            sys.executable,
            str(script),
            "--repo",
            str(repository),
            "validate",
            "--base",
            base_sha,
        ),
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "check=uv lock" in completed.stdout
    assert "ModuleNotFoundError" not in completed.stderr
