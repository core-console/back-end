"""Behavior tests for the repository-local agent harness."""

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.agent_harness as agent_harness
from scripts.agent_harness import (
    GitHubCli,
    PublicationError,
    ReviewSnapshotStaleError,
    ValidationCheck,
    collect_preflight,
    create_review_state,
    format_validation_result,
    snapshot_repository,
    validate_repository,
)

TEST_REPOSITORY_URL = "https://github.com/core-console/back-end.git"


class UnexpectedGitHub:
    """Fail if a pre-push rejection reaches the GitHub boundary."""

    def repository_identity(self) -> str:
        raise AssertionError("unexpected GitHub call: repository_identity")

    def list_validation_runs(
        self,
        branch: str,
        *,
        timeout_seconds: float | None = None,
    ) -> list[dict[str, object]]:
        del timeout_seconds
        raise AssertionError(f"unexpected GitHub call: list_validation_runs({branch})")

    def get_validation_run(
        self,
        run_id: int,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        del timeout_seconds
        raise AssertionError(f"unexpected GitHub call: get_validation_run({run_id})")

    def get_issue(self, issue: int) -> dict[str, object]:
        raise AssertionError(f"unexpected GitHub call: get_issue({issue})")

    def close_issue(self, issue: int) -> None:
        raise AssertionError(f"unexpected GitHub call: close_issue({issue})")


class FakeGitHub:
    """Deterministic GitHub boundary for publication behavior tests."""

    def __init__(
        self,
        *,
        runs: list[dict[str, object]],
        run_views: list[dict[str, object]],
        issue_number: int = 15,
        issue_state: str = "OPEN",
    ) -> None:
        self.runs = runs
        self.run_views = run_views
        self.issue_number = issue_number
        self.issue_state = issue_state
        self.closed_issues: list[int] = []
        self.operations: list[str] = []

    def repository_identity(self) -> str:
        return "core-console/back-end"

    def list_validation_runs(
        self,
        branch: str,
        *,
        timeout_seconds: float | None = None,
    ) -> list[dict[str, object]]:
        del timeout_seconds
        assert branch == "main"
        self.operations.append("list_validation_runs")
        return self.runs

    def get_validation_run(
        self,
        run_id: int,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        del timeout_seconds
        assert run_id == 202
        self.operations.append("get_validation_run")
        if len(self.run_views) > 1:
            return self.run_views.pop(0)
        return self.run_views[0]

    def get_issue(self, issue: int) -> dict[str, object]:
        self.operations.append("get_issue")
        return {"number": self.issue_number, "state": self.issue_state}

    def close_issue(self, issue: int) -> None:
        self.operations.append("close_issue")
        self.closed_issues.append(issue)
        self.issue_state = "CLOSED"


def validation_run(
    sha: str,
    *,
    run_id: int = 202,
    status: str = "completed",
    conclusion: str | None = "success",
) -> dict[str, object]:
    """Build one GitHub Actions run response."""

    return {
        "databaseId": run_id,
        "url": f"https://example.test/actions/runs/{run_id}",
        "name": "Validate",
        "workflowName": "Validate",
        "status": status,
        "conclusion": conclusion,
        "headSha": sha,
        "jobs": [
            {
                "name": "validate",
                "status": status,
                "conclusion": conclusion,
            }
        ],
    }


def object_dict(value: object) -> dict[str, object]:
    """Narrow one nested publication object for strict test typing."""

    assert isinstance(value, dict)
    return value


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


class IsolatedPublicationGit:
    """Route verified publication URLs to the test repository's isolated bare remote."""

    def __init__(self, repository: Path, expected_url: str = TEST_REPOSITORY_URL) -> None:
        self._repository = repository
        self._remote = repository.parent / "remote.git"
        self._expected_url = expected_url

    def live_branch_sha(self, remote_url: str, branch: str, *, phase: str) -> str:
        del phase
        assert remote_url == self._expected_url
        return git(self._remote, "rev-parse", f"refs/heads/{branch}")

    def push(self, remote_url: str, approved_sha: str, branch: str) -> None:
        assert remote_url == self._expected_url
        git(self._repository, "push", str(self._remote), f"{approved_sha}:refs/heads/{branch}")


def publish_repository(
    repository: Path,
    *,
    issue: int,
    base: str,
    approved_sha: str,
    branch: str,
    github: agent_harness.GitHubBoundary,
    publication_git: agent_harness.PublicationGitBoundary | None = None,
    ci_timeout_seconds: float = 1200,
    ci_poll_seconds: float = 10,
) -> dict[str, object]:
    """Exercise publication with real isolated Git state and no network writes."""

    return agent_harness.publish_repository(
        repository,
        issue=issue,
        base=base,
        approved_sha=approved_sha,
        branch=branch,
        github=github,
        publication_git=publication_git or IsolatedPublicationGit(repository),
        ci_timeout_seconds=ci_timeout_seconds,
        ci_poll_seconds=ci_poll_seconds,
    )


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


def committed_publication_candidate(tmp_path: Path) -> tuple[Path, str, str]:
    """Create one approved local commit above an isolated remote base."""

    repository, base_sha = initialized_repository(tmp_path)
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(repository, "remote", "add", "origin", TEST_REPOSITORY_URL)
    git(repository, "push", str(remote), "main:main")
    git(repository, "update-ref", "refs/remotes/origin/main", base_sha)
    git(repository, "config", "branch.main.remote", "origin")
    git(repository, "config", "branch.main.merge", "refs/heads/main")
    (repository / "implementation.txt").write_text("approved\n", encoding="utf-8")
    git(repository, "add", "implementation.txt")
    git(repository, "commit", "-m", "test: create approved candidate")
    return repository, base_sha, git(repository, "rev-parse", "HEAD")


def test_publish_rejects_mismatched_origin_push_destination(tmp_path: Path) -> None:
    """A push URL for another repository is rejected before either remote changes."""

    repository, base_sha, approved_sha = committed_publication_candidate(tmp_path)
    write_publication_evidence(repository, base_sha, protected=True, passing=True)
    other_remote = tmp_path / "other.git"
    git(tmp_path, "init", "--bare", str(other_remote))
    other_url = "https://github.com/other-owner/other-repository.git"
    git(repository, "config", f"url.{other_remote.resolve().as_uri()}.insteadOf", other_url)
    git(repository, "remote", "set-url", "--add", "--push", "origin", other_url)

    with pytest.raises(PublicationError, match="push destination"):
        publish_repository(
            repository,
            issue=15,
            base=base_sha,
            approved_sha=approved_sha,
            branch="main",
            github=FakeGitHub(
                runs=[validation_run(approved_sha)],
                run_views=[validation_run(approved_sha)],
            ),
            ci_timeout_seconds=1,
            ci_poll_seconds=0,
        )

    assert git(tmp_path / "remote.git", "rev-parse", "refs/heads/main") == base_sha
    assert (
        subprocess.run(
            ("git", "rev-parse", "--verify", "refs/heads/main"),
            cwd=other_remote,
            check=False,
            capture_output=True,
        ).returncode
        != 0
    )


def test_publish_rejects_multiple_origin_push_destinations(tmp_path: Path) -> None:
    """More than one effective push URL is ambiguous and blocks publication."""

    repository, base_sha, approved_sha = committed_publication_candidate(tmp_path)
    write_publication_evidence(repository, base_sha, protected=True, passing=True)
    other_remote = tmp_path / "other.git"
    git(tmp_path, "init", "--bare", str(other_remote))
    other_url = "https://github.com/core-console/back-end-mirror.git"
    git(repository, "config", f"url.{other_remote.resolve().as_uri()}.insteadOf", other_url)
    git(repository, "remote", "set-url", "--add", "--push", "origin", TEST_REPOSITORY_URL)
    git(repository, "remote", "set-url", "--add", "--push", "origin", other_url)

    with pytest.raises(PublicationError, match=r"exactly one.*push destination"):
        publish_repository(
            repository,
            issue=15,
            base=base_sha,
            approved_sha=approved_sha,
            branch="main",
            github=FakeGitHub(
                runs=[validation_run(approved_sha)],
                run_views=[validation_run(approved_sha)],
            ),
            ci_timeout_seconds=1,
            ci_poll_seconds=0,
        )

    assert git(tmp_path / "remote.git", "rev-parse", "refs/heads/main") == base_sha
    assert (
        subprocess.run(
            ("git", "rev-parse", "--verify", "refs/heads/main"),
            cwd=other_remote,
            check=False,
            capture_output=True,
        ).returncode
        != 0
    )


def test_publish_rejects_push_instead_of_redirect_before_external_action(
    tmp_path: Path,
) -> None:
    """Git push rewriting cannot redirect an approved GitHub destination."""

    repository, base_sha, approved_sha = committed_publication_candidate(tmp_path)
    write_publication_evidence(repository, base_sha, protected=True, passing=True)
    redirected_remote = tmp_path / "redirected.git"
    git(tmp_path, "init", "--bare", str(redirected_remote))
    git(
        repository,
        "config",
        f"url.{redirected_remote.resolve().as_uri()}.pushInsteadOf",
        TEST_REPOSITORY_URL,
    )
    github = FakeGitHub(runs=[validation_run(approved_sha)], run_views=[])

    with pytest.raises(PublicationError, match="effective push destination"):
        publish_repository(
            repository,
            issue=15,
            base=base_sha,
            approved_sha=approved_sha,
            branch="main",
            github=github,
        )

    assert git(tmp_path / "remote.git", "rev-parse", "refs/heads/main") == base_sha
    assert (
        subprocess.run(
            ("git", "rev-parse", "--verify", "refs/heads/main"),
            cwd=redirected_remote,
            check=False,
            capture_output=True,
        ).returncode
        != 0
    )
    assert github.operations == []


def test_publish_rejects_rewrite_chain_before_external_action(tmp_path: Path) -> None:
    """A verified URL must not be rewritable again when passed to Git push."""

    repository, base_sha, approved_sha = committed_publication_candidate(tmp_path)
    write_publication_evidence(repository, base_sha, protected=True, passing=True)
    fetch_remote = tmp_path / "remote.git"
    redirected_remote = tmp_path / "redirected.git"
    git(tmp_path, "init", "--bare", str(redirected_remote))
    same_repository_ssh = "git@github.com:core-console/back-end.git"
    git(
        repository,
        "config",
        f"url.{fetch_remote.resolve().as_uri()}.insteadOf",
        TEST_REPOSITORY_URL,
    )
    git(
        repository,
        "config",
        f"url.{same_repository_ssh}.pushInsteadOf",
        TEST_REPOSITORY_URL,
    )
    git(
        repository,
        "config",
        f"url.{redirected_remote.resolve().as_uri()}.insteadOf",
        same_repository_ssh,
    )
    github = FakeGitHub(runs=[validation_run(approved_sha)], run_views=[])

    with pytest.raises(PublicationError, match="rewrite-stable"):
        agent_harness.publish_repository(
            repository,
            issue=15,
            base=base_sha,
            approved_sha=approved_sha,
            branch="main",
            github=github,
        )

    assert git(fetch_remote, "rev-parse", "refs/heads/main") == base_sha
    assert (
        subprocess.run(
            ("git", "rev-parse", "--verify", "refs/heads/main"),
            cwd=redirected_remote,
            check=False,
            capture_output=True,
        ).returncode
        != 0
    )
    assert github.operations == []


def test_publish_uses_single_verified_matching_push_destination(tmp_path: Path) -> None:
    """One explicit matching push URL remains a valid normal publication path."""

    repository, base_sha, approved_sha = committed_publication_candidate(tmp_path)
    git(repository, "remote", "set-url", "--add", "--push", "origin", TEST_REPOSITORY_URL)
    write_publication_evidence(repository, base_sha, protected=True, passing=True)
    github = FakeGitHub(
        runs=[validation_run(approved_sha)],
        run_views=[validation_run(approved_sha)],
    )

    result = publish_repository(
        repository,
        issue=15,
        base=base_sha,
        approved_sha=approved_sha,
        branch="main",
        github=github,
        ci_timeout_seconds=1,
        ci_poll_seconds=0,
    )

    assert git(tmp_path / "remote.git", "rev-parse", "refs/heads/main") == approved_sha
    assert github.closed_issues == [15]
    assert result["outcome"] == "PASS"


def test_publish_allows_rewrite_stable_ssh_destination(tmp_path: Path) -> None:
    """A normal supported SSH URL remains publishable when Git leaves it unchanged."""

    repository, base_sha, approved_sha = committed_publication_candidate(tmp_path)
    ssh_url = "git@github.com:core-console/back-end.git"
    git(repository, "remote", "set-url", "origin", ssh_url)
    write_publication_evidence(repository, base_sha, protected=True, passing=True)
    github = FakeGitHub(
        runs=[validation_run(approved_sha)],
        run_views=[validation_run(approved_sha)],
    )

    result = publish_repository(
        repository,
        issue=15,
        base=base_sha,
        approved_sha=approved_sha,
        branch="main",
        github=github,
        publication_git=IsolatedPublicationGit(repository, ssh_url),
        ci_timeout_seconds=1,
        ci_poll_seconds=0,
    )

    assert git(tmp_path / "remote.git", "rev-parse", "refs/heads/main") == approved_sha
    assert github.closed_issues == [15]
    assert result["outcome"] == "PASS"


def write_publication_evidence(
    repository: Path,
    base_sha: str,
    *,
    protected: bool,
    passing: bool,
) -> Path:
    """Create real snapshot-bound validation and review receipts for a candidate."""

    environment = os.environ.copy()
    if protected:
        environment["TEST_DATABASE_URL"] = "postgresql+psycopg://test.invalid/core_test"
    check = ValidationCheck(
        "pytest",
        (sys.executable, "-c", f"raise SystemExit({0 if passing else 1})"),
    )
    validation = validate_repository(
        repository,
        base_sha,
        protected=protected,
        checks=(check,),
        environment=environment,
    )
    create_review_state(repository, base_sha)
    return validation.receipt_path


def test_publish_rejects_wrong_head_before_external_action(tmp_path: Path) -> None:
    """Explicit approval is bound to the current HEAD SHA."""

    repository, base_sha, _approved_sha = committed_publication_candidate(tmp_path)

    with pytest.raises(PublicationError, match="HEAD does not equal approved SHA"):
        publish_repository(
            repository,
            issue=15,
            base=base_sha,
            approved_sha="0" * 40,
            branch="main",
            github=UnexpectedGitHub(),
        )

    assert git(tmp_path / "remote.git", "rev-parse", "refs/heads/main") == base_sha


def test_publish_rejects_dirty_worktree_before_external_action(tmp_path: Path) -> None:
    """Publication cannot race unapproved worktree content."""

    repository, base_sha, approved_sha = committed_publication_candidate(tmp_path)
    (repository / "unapproved.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(PublicationError, match="worktree is not clean"):
        publish_repository(
            repository,
            issue=15,
            base=base_sha,
            approved_sha=approved_sha,
            branch="main",
            github=UnexpectedGitHub(),
        )

    assert git(tmp_path / "remote.git", "rev-parse", "refs/heads/main") == base_sha


def test_publish_rejects_diverged_local_tracking_state(tmp_path: Path) -> None:
    """A local tracking ref outside the expected publication edge is unsafe."""

    repository, base_sha, approved_sha = committed_publication_candidate(tmp_path)
    divergent_sha = git(
        repository,
        "commit-tree",
        f"{base_sha}^{{tree}}",
        "-p",
        base_sha,
        "-m",
        "test: divergent remote",
    )
    git(repository, "update-ref", "refs/remotes/origin/main", divergent_sha)

    with pytest.raises(PublicationError, match="local origin tracking state"):
        publish_repository(
            repository,
            issue=15,
            base=base_sha,
            approved_sha=approved_sha,
            branch="main",
            github=UnexpectedGitHub(),
        )


def test_publish_rejects_local_tracking_state_behind_expected_base(tmp_path: Path) -> None:
    """A stale local tracking ref cannot authorize a later base and candidate."""

    repository, initial_sha = initialized_repository(tmp_path)
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(repository, "remote", "add", "origin", str(remote))
    git(repository, "push", "-u", "origin", "main")
    (repository / "implementation.txt").write_text("base\n", encoding="utf-8")
    git(repository, "add", "implementation.txt")
    git(repository, "commit", "-m", "test: establish later base")
    base_sha = git(repository, "rev-parse", "HEAD")
    git(repository, "push", "origin", "main")
    (repository / "implementation.txt").write_text("approved\n", encoding="utf-8")
    git(repository, "add", "implementation.txt")
    git(repository, "commit", "-m", "test: create approved candidate")
    approved_sha = git(repository, "rev-parse", "HEAD")
    git(repository, "update-ref", "refs/remotes/origin/main", initial_sha)

    with pytest.raises(PublicationError, match="local origin tracking state"):
        publish_repository(
            repository,
            issue=15,
            base=base_sha,
            approved_sha=approved_sha,
            branch="main",
            github=UnexpectedGitHub(),
        )


@pytest.mark.parametrize(
    ("protected", "passing", "message"),
    (
        pytest.param(True, False, "validation outcome is not PASS", id="matching-fail"),
        pytest.param(
            False,
            True,
            "protected PostgreSQL evidence is required",
            id="matching-unprotected-pass",
        ),
    ),
)
def test_publish_rejects_inadequate_matching_validation(
    tmp_path: Path,
    protected: bool,
    passing: bool,
    message: str,
) -> None:
    """Matching identity does not hide failure or missing protected coverage."""

    repository, base_sha, approved_sha = committed_publication_candidate(tmp_path)
    write_publication_evidence(
        repository,
        base_sha,
        protected=protected,
        passing=passing,
    )

    with pytest.raises(PublicationError, match=message):
        publish_repository(
            repository,
            issue=15,
            base=base_sha,
            approved_sha=approved_sha,
            branch="main",
            github=UnexpectedGitHub(),
        )


def test_publish_rejects_stale_validation_receipt(tmp_path: Path) -> None:
    """A receipt filename cannot substitute for exact structured identity."""

    repository, base_sha, approved_sha = committed_publication_candidate(tmp_path)
    receipt_path = write_publication_evidence(
        repository,
        base_sha,
        protected=True,
        passing=True,
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["headSha"] = "0" * 40
    receipt_path.write_text(f"{json.dumps(receipt)}\n", encoding="utf-8")

    with pytest.raises(PublicationError, match="validation receipt is stale"):
        publish_repository(
            repository,
            issue=15,
            base=base_sha,
            approved_sha=approved_sha,
            branch="main",
            github=UnexpectedGitHub(),
        )


def test_publish_rejects_live_remote_drift(tmp_path: Path) -> None:
    """The live remote must still be the expected base immediately before push."""

    repository, base_sha, approved_sha = committed_publication_candidate(tmp_path)
    write_publication_evidence(repository, base_sha, protected=True, passing=True)
    divergent_sha = git(
        repository,
        "commit-tree",
        f"{base_sha}^{{tree}}",
        "-p",
        base_sha,
        "-m",
        "test: live remote drift",
    )
    remote = tmp_path / "remote.git"
    git(repository, "push", str(remote), f"{divergent_sha}:refs/heads/drift-fixture")
    git(remote, "update-ref", "refs/heads/main", divergent_sha)

    with pytest.raises(PublicationError, match="live remote branch drifted"):
        publish_repository(
            repository,
            issue=15,
            base=base_sha,
            approved_sha=approved_sha,
            branch="main",
            github=UnexpectedGitHub(),
        )

    assert git(remote, "rev-parse", "refs/heads/main") == divergent_sha


def test_publish_ls_remote_failure_is_compact_and_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Large live-remote stderr stays in a publication Git log."""

    repository, base_sha, approved_sha = committed_publication_candidate(tmp_path)
    write_publication_evidence(repository, base_sha, protected=True, passing=True)
    original_run = subprocess.run
    marker = "large-ls-remote-diagnostic"

    def failing_ls_remote(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        if command[:3] == ("git", "ls-remote", "--exit-code"):
            return subprocess.CompletedProcess(
                command,
                2,
                b"",
                (marker + "\n" + "remote detail\n" * 5000).encode(),
            )
        return original_run(command, **kwargs)  # type: ignore[no-any-return,call-overload]

    monkeypatch.setattr(subprocess, "run", failing_ls_remote)

    with pytest.raises(PublicationError) as error:
        agent_harness.publish_repository(
            repository,
            issue=15,
            base=base_sha,
            approved_sha=approved_sha,
            branch="main",
            github=UnexpectedGitHub(),
        )

    assert len(str(error.value)) < 300
    assert marker not in str(error.value)
    git_logs = list(
        (repository / ".agent" / "logs" / "publication" / f"issue-15-{approved_sha}").glob(
            "*git*.log"
        )
    )
    assert len(git_logs) == 1
    assert marker in git_logs[0].read_text(encoding="utf-8")


def test_publication_remote_read_recovers_from_transient_transport_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A classified transient read failure is retried with a fresh transcript."""

    expected_sha = "a" * 40
    attempts = 0

    def transient_then_success(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal attempts
        attempts += 1
        assert kwargs.get("timeout") is not None
        if attempts == 1:
            return subprocess.CompletedProcess(
                command,
                128,
                b"",
                b"OpenSSL SSL_read: unexpected eof while reading",
            )
        return subprocess.CompletedProcess(
            command,
            0,
            f"{expected_sha}\trefs/heads/main\n".encode(),
            b"",
        )

    monkeypatch.setattr(subprocess, "run", transient_then_success)
    logs = tmp_path / "logs"

    assert (
        agent_harness.PublicationGit(tmp_path, logs).live_branch_sha(
            TEST_REPOSITORY_URL,
            "main",
            phase="live-remote",
        )
        == expected_sha
    )
    assert attempts == 2
    assert len(list(logs.glob("*-live-remote-git.log"))) == 2


def test_publication_remote_read_exhausts_small_transient_attempt_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated transient failures stop after the fixed small attempt count."""

    attempts = 0

    def transient_failure(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal attempts
        attempts += 1
        return subprocess.CompletedProcess(
            command,
            128,
            b"",
            b"schannel: failed to receive handshake",
        )

    monkeypatch.setattr(subprocess, "run", transient_failure)
    logs = tmp_path / "logs"

    with pytest.raises(PublicationError, match=r"transient.*attempts"):
        agent_harness.PublicationGit(tmp_path, logs).live_branch_sha(
            TEST_REPOSITORY_URL,
            "main",
            phase="live-remote",
        )

    assert attempts == agent_harness.PUBLICATION_REMOTE_READ_ATTEMPTS
    assert len(list(logs.glob("*-live-remote-git.log"))) == attempts


def test_publication_remote_read_timeouts_share_one_overall_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each blocked read is bounded by the remaining shared deadline."""

    monotonic_values = iter((100.0, 100.0, 100.03, 100.051))
    timeouts: list[float] = []

    def monotonic() -> float:
        return next(monotonic_values)

    def blocked_read(
        command: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        timeout = kwargs.get("timeout")
        assert isinstance(timeout, float)
        timeouts.append(timeout)
        raise subprocess.TimeoutExpired(command, timeout, output=b"partial remote output")

    monkeypatch.setattr(agent_harness, "PUBLICATION_REMOTE_READ_DEADLINE_SECONDS", 0.05)
    monkeypatch.setattr(time, "monotonic", monotonic)
    monkeypatch.setattr(subprocess, "run", blocked_read)
    logs = tmp_path / "logs"

    with pytest.raises(PublicationError, match="deadline"):
        agent_harness.PublicationGit(tmp_path, logs).live_branch_sha(
            TEST_REPOSITORY_URL,
            "main",
            phase="live-remote",
        )

    assert timeouts == pytest.approx([0.05, 0.02])
    assert len(list(logs.glob("*-live-remote-git.log"))) == 2
    assert "partial remote output" in (logs / "01-live-remote-git.log").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "stderr",
    [
        b"fatal: Authentication failed for 'https://github.com/core-console/back-end.git/'",
        b"fatal: unable to access remote: SSL certificate problem: unable to get local issuer",
    ],
)
def test_publication_remote_read_does_not_retry_non_transient_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stderr: bytes,
) -> None:
    """Authentication and certificate failures fail on their first attempt."""

    attempts = 0

    def non_transient_failure(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal attempts
        attempts += 1
        return subprocess.CompletedProcess(command, 128, b"", stderr)

    monkeypatch.setattr(subprocess, "run", non_transient_failure)

    with pytest.raises(PublicationError, match="failed"):
        agent_harness.PublicationGit(tmp_path, tmp_path / "logs").live_branch_sha(
            TEST_REPOSITORY_URL,
            "main",
            phase="live-remote",
        )

    assert attempts == 1


@pytest.mark.parametrize(
    "stderr",
    [
        b"fatal: Authentication failed while the connection timed out",
        b"SSL certificate problem: unable to get local issuer after operation timed out",
    ],
)
def test_publication_remote_read_timeout_does_not_override_permanent_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stderr: bytes,
) -> None:
    """Permanent diagnostics accompanying a timeout take precedence over retry."""

    attempts = 0

    def permanent_timeout(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal attempts
        attempts += 1
        timeout = kwargs.get("timeout")
        assert isinstance(timeout, float)
        raise subprocess.TimeoutExpired(command, timeout, stderr=stderr)

    monkeypatch.setattr(subprocess, "run", permanent_timeout)

    with pytest.raises(PublicationError, match="failed"):
        agent_harness.PublicationGit(tmp_path, tmp_path / "logs").live_branch_sha(
            TEST_REPOSITORY_URL,
            "main",
            phase="live-remote",
        )

    assert attempts == 1


def test_publication_remote_read_does_not_retry_malformed_success_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful command with ambiguous semantic evidence fails immediately."""

    attempts = 0

    def malformed_success(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal attempts
        attempts += 1
        return subprocess.CompletedProcess(command, 0, b"not-a-ref", b"")

    monkeypatch.setattr(subprocess, "run", malformed_success)

    with pytest.raises(PublicationError, match="ambiguous branch evidence"):
        agent_harness.PublicationGit(tmp_path, tmp_path / "logs").live_branch_sha(
            TEST_REPOSITORY_URL,
            "main",
            phase="live-remote",
        )

    assert attempts == 1


def test_publish_rejected_push_diagnostics_are_compact_and_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rejected normal-push output is retained without flooding the caller."""

    repository, base_sha, approved_sha = committed_publication_candidate(tmp_path)
    write_publication_evidence(repository, base_sha, protected=True, passing=True)
    original_run = subprocess.run
    push_attempts = 0
    stdout_marker = "rejected-push-stdout"
    stderr_marker = "rejected-push-stderr"

    def rejecting_push(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal push_attempts
        if command[:3] == ("git", "ls-remote", "--exit-code"):
            return subprocess.CompletedProcess(
                command,
                0,
                f"{base_sha}\trefs/heads/main\n".encode(),
                b"",
            )
        if command[:2] == ("git", "push"):
            push_attempts += 1
            return subprocess.CompletedProcess(
                command,
                1,
                (stdout_marker + "\n" + "stdout detail\n" * 5000).encode(),
                (stderr_marker + "\n" + "stderr detail\n" * 5000).encode(),
            )
        return original_run(command, **kwargs)  # type: ignore[no-any-return,call-overload]

    monkeypatch.setattr(subprocess, "run", rejecting_push)
    github = FakeGitHub(runs=[], run_views=[])

    with pytest.raises(PublicationError, match="push") as error:
        agent_harness.publish_repository(
            repository,
            issue=15,
            base=base_sha,
            approved_sha=approved_sha,
            branch="main",
            github=github,
        )

    assert len(str(error.value)) < 300
    assert stdout_marker not in str(error.value)
    assert stderr_marker not in str(error.value)
    log_directory = repository / ".agent" / "logs" / "publication" / f"issue-15-{approved_sha}"
    push_logs = list(log_directory.glob("??-push-git.log"))
    assert len(push_logs) == 1
    stored = push_logs[0].read_text(encoding="utf-8")
    assert stdout_marker in stored
    assert stderr_marker in stored
    assert push_attempts == 1
    assert git(tmp_path / "remote.git", "rev-parse", "refs/heads/main") == base_sha
    assert github.closed_issues == []


def test_publish_uses_normal_push_and_closes_only_after_exact_sha_ci(
    tmp_path: Path,
) -> None:
    """A protected candidate publishes through the exact CI run and supplied issue."""

    repository, base_sha, approved_sha = committed_publication_candidate(tmp_path)
    write_publication_evidence(repository, base_sha, protected=True, passing=True)
    github = FakeGitHub(
        runs=[
            validation_run(base_sha, run_id=101),
            validation_run(approved_sha, status="queued", conclusion=None),
        ],
        run_views=[
            validation_run(approved_sha, status="in_progress", conclusion=None),
            validation_run(approved_sha),
        ],
    )

    result = publish_repository(
        repository,
        issue=15,
        base=base_sha,
        approved_sha=approved_sha,
        branch="main",
        github=github,
        ci_timeout_seconds=1,
        ci_poll_seconds=0,
    )

    assert git(tmp_path / "remote.git", "rev-parse", "refs/heads/main") == approved_sha
    assert github.closed_issues == [15]
    assert result["outcome"] == "PASS"
    assert result["pushedSha"] == approved_sha
    ci = object_dict(result["ci"])
    push = object_dict(result["push"])
    assert ci["runId"] == 202
    assert ci["headSha"] == approved_sha
    assert result["finalIssueState"] == "CLOSED"
    assert push["mode"] == "normal-fast-forward"
    assert "force" not in json.dumps(result).casefold()
    receipt = json.loads((repository / str(result["receiptPath"])).read_text(encoding="utf-8"))
    assert receipt["schema"] == "core-console-agent-publication/v1"
    validation_receipt = object_dict(receipt["validationReceipt"])
    review_receipt = object_dict(receipt["reviewReceipt"])
    assert validation_receipt["snapshotDigest"] == review_receipt["snapshotDigest"]
    assert receipt["remoteBranchBeforePush"] == base_sha
    assert receipt["remoteBranchAfterPush"] == approved_sha
    assert receipt["workingTreeClean"] is True


@pytest.mark.parametrize("conclusion", ("failure", "cancelled"))
def test_failed_or_cancelled_exact_sha_ci_blocks_issue_closure(
    tmp_path: Path,
    conclusion: str,
) -> None:
    """A terminal non-success conclusion stops after push and preserves the issue."""

    repository, base_sha, approved_sha = committed_publication_candidate(tmp_path)
    write_publication_evidence(repository, base_sha, protected=True, passing=True)
    github = FakeGitHub(
        runs=[validation_run(approved_sha, conclusion=conclusion)],
        run_views=[validation_run(approved_sha, conclusion=conclusion)],
    )

    with pytest.raises(PublicationError, match=f"CI concluded {conclusion}"):
        publish_repository(
            repository,
            issue=15,
            base=base_sha,
            approved_sha=approved_sha,
            branch="main",
            github=github,
            ci_timeout_seconds=1,
            ci_poll_seconds=0,
        )

    assert git(tmp_path / "remote.git", "rev-parse", "refs/heads/main") == approved_sha
    assert github.closed_issues == []
    receipt_path = repository / ".agent" / "receipts" / f"publication-issue-15-{approved_sha}.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["outcome"] == "FAIL"
    assert receipt["failedStep"] == "exact-SHA CI"
    assert object_dict(receipt["ci"])["headSha"] == approved_sha
    assert receipt["finalIssueState"] == "not queried"


@pytest.mark.parametrize(
    "case",
    (
        "selected-run-view-returns-older-id",
        "selected-run-view-returns-other-id",
        "validate-job-failure",
        "validate-job-cancelled",
        "missing-jobs",
        "malformed-jobs",
        "missing-validate-job",
        "incomplete-validate-job",
    ),
)
def test_invalid_exact_sha_ci_evidence_blocks_issue_closure(
    tmp_path: Path,
    case: str,
) -> None:
    """Every selected-run and required-job proof is complete before closure."""

    repository, base_sha, approved_sha = committed_publication_candidate(tmp_path)
    write_publication_evidence(repository, base_sha, protected=True, passing=True)
    detailed_run = validation_run(approved_sha)
    if case == "selected-run-view-returns-older-id":
        detailed_run["databaseId"] = 201
    elif case == "selected-run-view-returns-other-id":
        detailed_run["databaseId"] = 203
    elif case == "validate-job-failure":
        detailed_run["jobs"] = [
            {"name": "validate", "status": "completed", "conclusion": "failure"}
        ]
    elif case == "validate-job-cancelled":
        detailed_run["jobs"] = [
            {"name": "validate", "status": "completed", "conclusion": "cancelled"}
        ]
    elif case == "missing-jobs":
        del detailed_run["jobs"]
    elif case == "malformed-jobs":
        detailed_run["jobs"] = "not-a-job-list"
    elif case == "missing-validate-job":
        detailed_run["jobs"] = [{"name": "other", "status": "completed", "conclusion": "success"}]
    elif case == "incomplete-validate-job":
        detailed_run["jobs"] = [{"name": "validate", "status": "in_progress", "conclusion": None}]
    github = FakeGitHub(
        runs=[validation_run(approved_sha)],
        run_views=[detailed_run],
    )

    with pytest.raises(PublicationError, match="exact-SHA CI"):
        publish_repository(
            repository,
            issue=15,
            base=base_sha,
            approved_sha=approved_sha,
            branch="main",
            github=github,
            ci_timeout_seconds=1,
            ci_poll_seconds=0,
        )

    assert github.closed_issues == []
    receipt_path = repository / ".agent" / "receipts" / f"publication-issue-15-{approved_sha}.json"
    if receipt_path.exists():
        assert json.loads(receipt_path.read_text(encoding="utf-8"))["outcome"] != "PASS"


def test_publish_rejects_wrong_issue_response_without_closing(tmp_path: Path) -> None:
    """Only the explicitly supplied issue number can be closed."""

    repository, base_sha, approved_sha = committed_publication_candidate(tmp_path)
    write_publication_evidence(repository, base_sha, protected=True, passing=True)
    github = FakeGitHub(
        runs=[validation_run(approved_sha)],
        run_views=[validation_run(approved_sha)],
        issue_number=14,
    )

    with pytest.raises(PublicationError, match="different issue"):
        publish_repository(
            repository,
            issue=15,
            base=base_sha,
            approved_sha=approved_sha,
            branch="main",
            github=github,
            ci_timeout_seconds=1,
            ci_poll_seconds=0,
        )

    assert github.closed_issues == []


def test_publish_rechecks_remote_immediately_before_issue_closure(tmp_path: Path) -> None:
    """Remote drift during issue lookup prevents closure and PASS evidence."""

    repository, base_sha, approved_sha = committed_publication_candidate(tmp_path)
    write_publication_evidence(repository, base_sha, protected=True, passing=True)
    divergent_sha = git(
        repository,
        "commit-tree",
        f"{base_sha}^{{tree}}",
        "-p",
        base_sha,
        "-m",
        "test: drift during issue lookup",
    )
    git(
        repository,
        "push",
        str(tmp_path / "remote.git"),
        f"{divergent_sha}:refs/heads/drift-fixture",
    )
    remote = tmp_path / "remote.git"
    github = FakeGitHub(
        runs=[validation_run(approved_sha)],
        run_views=[validation_run(approved_sha)],
    )
    original_get_issue = github.get_issue
    issue_lookups = 0

    def drifting_get_issue(issue: int) -> dict[str, object]:
        nonlocal issue_lookups
        issue_lookups += 1
        if issue_lookups == 1:
            git(remote, "update-ref", "refs/heads/main", divergent_sha)
        return original_get_issue(issue)

    github.get_issue = drifting_get_issue  # type: ignore[method-assign]

    with pytest.raises(PublicationError, match="immediately before issue closure"):
        publish_repository(
            repository,
            issue=15,
            base=base_sha,
            approved_sha=approved_sha,
            branch="main",
            github=github,
            ci_timeout_seconds=1,
            ci_poll_seconds=0,
        )

    assert issue_lookups == 1
    assert git(remote, "rev-parse", "refs/heads/main") == divergent_sha
    assert github.closed_issues == []
    receipt_path = repository / ".agent" / "receipts" / f"publication-issue-15-{approved_sha}.json"
    if receipt_path.exists():
        assert json.loads(receipt_path.read_text(encoding="utf-8"))["outcome"] != "PASS"


def test_publish_rerun_does_not_push_or_close_again(tmp_path: Path) -> None:
    """A completed publication is safely verified on rerun."""

    repository, base_sha, approved_sha = committed_publication_candidate(tmp_path)
    write_publication_evidence(repository, base_sha, protected=True, passing=True)
    github = FakeGitHub(
        runs=[validation_run(approved_sha)],
        run_views=[validation_run(approved_sha)],
    )
    first = publish_repository(
        repository,
        issue=15,
        base=base_sha,
        approved_sha=approved_sha,
        branch="main",
        github=github,
        ci_timeout_seconds=1,
        ci_poll_seconds=0,
    )
    second = publish_repository(
        repository,
        issue=15,
        base=base_sha,
        approved_sha=approved_sha,
        branch="main",
        github=github,
        ci_timeout_seconds=1,
        ci_poll_seconds=0,
    )

    assert object_dict(first["push"])["performed"] is True
    assert object_dict(second["push"])["performed"] is False
    assert github.closed_issues == [15]
    assert git(repository, "rev-list", "--count", f"{base_sha}..{approved_sha}") == "1"


def test_publish_rerun_can_finish_closure_after_exact_sha_ci(tmp_path: Path) -> None:
    """A rerun after an interrupted closure resumes without another push."""

    repository, base_sha, approved_sha = committed_publication_candidate(tmp_path)
    write_publication_evidence(repository, base_sha, protected=True, passing=True)
    github = FakeGitHub(
        runs=[validation_run(approved_sha)],
        run_views=[validation_run(approved_sha)],
        issue_state="OPEN",
    )
    original_get_issue = github.get_issue
    calls = 0

    def interrupted_get_issue(issue: int) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PublicationError("simulated interruption before closure")
        return original_get_issue(issue)

    github.get_issue = interrupted_get_issue  # type: ignore[method-assign]
    with pytest.raises(PublicationError, match="simulated interruption"):
        publish_repository(
            repository,
            issue=15,
            base=base_sha,
            approved_sha=approved_sha,
            branch="main",
            github=github,
            ci_timeout_seconds=1,
            ci_poll_seconds=0,
        )

    result = publish_repository(
        repository,
        issue=15,
        base=base_sha,
        approved_sha=approved_sha,
        branch="main",
        github=github,
        ci_timeout_seconds=1,
        ci_poll_seconds=0,
    )

    assert object_dict(result["push"])["performed"] is False
    assert github.closed_issues == [15]


def test_publish_selects_newest_exact_sha_run(tmp_path: Path) -> None:
    """Multiple exact-SHA runs resolve deterministically to the largest run ID."""

    repository, base_sha, approved_sha = committed_publication_candidate(tmp_path)
    write_publication_evidence(repository, base_sha, protected=True, passing=True)
    github = FakeGitHub(
        runs=[
            validation_run(approved_sha, run_id=201),
            validation_run(approved_sha, run_id=202, conclusion="failure"),
        ],
        run_views=[validation_run(approved_sha, run_id=202, conclusion="failure")],
    )

    with pytest.raises(PublicationError, match="CI concluded failure"):
        publish_repository(
            repository,
            issue=15,
            base=base_sha,
            approved_sha=approved_sha,
            branch="main",
            github=github,
            ci_timeout_seconds=1,
            ci_poll_seconds=0,
        )

    assert github.closed_issues == []


def test_publish_generates_only_normal_push_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The publication boundary never generates a force-capable push."""

    repository, base_sha, approved_sha = committed_publication_candidate(tmp_path)
    write_publication_evidence(repository, base_sha, protected=True, passing=True)
    github = FakeGitHub(
        runs=[validation_run(approved_sha)],
        run_views=[validation_run(approved_sha)],
    )
    original_run = subprocess.run
    publication_pushes: list[tuple[str, ...]] = []

    def recording_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        command = args[0]
        if isinstance(command, tuple) and kwargs.get("cwd") == repository:
            if (
                command[:3] == ("git", "ls-remote", "--exit-code")
                and command[3] == TEST_REPOSITORY_URL
            ):
                translated = (*command[:3], str(tmp_path / "remote.git"), *command[4:])
                return original_run(translated, **kwargs)  # type: ignore[no-any-return,call-overload]
            if command[:2] == ("git", "push"):
                publication_pushes.append(command)
                translated = (*command[:2], str(tmp_path / "remote.git"), *command[3:])
                return original_run(translated, **kwargs)  # type: ignore[no-any-return,call-overload]
        return original_run(*args, **kwargs)  # type: ignore[no-any-return,call-overload]

    monkeypatch.setattr(subprocess, "run", recording_run)
    agent_harness.publish_repository(
        repository,
        issue=15,
        base=base_sha,
        approved_sha=approved_sha,
        branch="main",
        github=github,
        ci_timeout_seconds=1,
        ci_poll_seconds=0,
    )

    assert publication_pushes == [
        (
            "git",
            "push",
            TEST_REPOSITORY_URL,
            f"{approved_sha}:refs/heads/main",
        )
    ]
    assert all("force" not in argument for command in publication_pushes for argument in command)


def test_publish_ci_discovery_timeout_retains_compact_failure(tmp_path: Path) -> None:
    """Missing exact-SHA CI stops with a receipt and without querying the issue."""

    repository, base_sha, approved_sha = committed_publication_candidate(tmp_path)
    write_publication_evidence(repository, base_sha, protected=True, passing=True)
    github = FakeGitHub(runs=[validation_run(base_sha)], run_views=[])

    with pytest.raises(PublicationError, match="could not be identified or timed out") as error:
        publish_repository(
            repository,
            issue=15,
            base=base_sha,
            approved_sha=approved_sha,
            branch="main",
            github=github,
            ci_timeout_seconds=0,
            ci_poll_seconds=0,
        )

    assert len(str(error.value)) < 300
    assert github.closed_issues == []


def test_publish_bounds_blocked_github_command_by_remaining_ci_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One blocked gh lookup cannot outlive the configured CI deadline."""

    repository, base_sha, approved_sha = committed_publication_candidate(tmp_path)
    write_publication_evidence(repository, base_sha, protected=True, passing=True)
    original_run = subprocess.run
    gh_commands: list[tuple[str, ...]] = []
    gh_started: list[float] = []
    marker = "blocked-github-command-diagnostic"

    def blocked_github_command(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        if command[:3] == ("gh", "run", "list"):
            gh_commands.append(command)
            gh_started.append(time.monotonic())
            timeout = kwargs.get("timeout")
            assert isinstance(timeout, float)
            assert 0 < timeout <= 0.05
            raise subprocess.TimeoutExpired(command, timeout, output=marker.encode())
        return original_run(command, **kwargs)  # type: ignore[no-any-return,call-overload]

    monkeypatch.setattr(subprocess, "run", blocked_github_command)
    logs = repository / ".agent" / "logs" / "publication-timeout"
    github = GitHubCli(repository, logs, "core-console/back-end")
    with pytest.raises(PublicationError, match="timed out") as error:
        publish_repository(
            repository,
            issue=15,
            base=base_sha,
            approved_sha=approved_sha,
            branch="main",
            github=github,
            ci_timeout_seconds=0.05,
            ci_poll_seconds=0,
        )

    assert time.monotonic() - gh_started[0] < 0.5
    assert len(str(error.value)) < 300
    assert len(gh_commands) == 1
    assert all(command[:2] != ("gh", "issue") for command in gh_commands)
    github_log = next(logs.glob("*.log"))
    assert marker in github_log.read_text(encoding="utf-8")
    assert marker not in str(error.value)


def test_publish_rerun_verifies_ambiguous_completed_closure(tmp_path: Path) -> None:
    """A close response failure is safe when a rerun observes the issue closed."""

    repository, base_sha, approved_sha = committed_publication_candidate(tmp_path)
    write_publication_evidence(repository, base_sha, protected=True, passing=True)
    github = FakeGitHub(
        runs=[validation_run(approved_sha)],
        run_views=[validation_run(approved_sha)],
    )
    original_close = github.close_issue

    def ambiguous_close(issue: int) -> None:
        original_close(issue)
        raise PublicationError("ambiguous close response")

    github.close_issue = ambiguous_close  # type: ignore[method-assign]
    with pytest.raises(PublicationError, match="ambiguous close response"):
        publish_repository(
            repository,
            issue=15,
            base=base_sha,
            approved_sha=approved_sha,
            branch="main",
            github=github,
            ci_timeout_seconds=1,
            ci_poll_seconds=0,
        )
    github.close_issue = original_close  # type: ignore[method-assign]

    result = publish_repository(
        repository,
        issue=15,
        base=base_sha,
        approved_sha=approved_sha,
        branch="main",
        github=github,
        ci_timeout_seconds=1,
        ci_poll_seconds=0,
    )

    assert result["finalIssueState"] == "CLOSED"
    assert object_dict(result["push"])["performed"] is False
    assert github.closed_issues == [15]


def test_publish_cli_requires_explicit_issue_base_sha_and_branch() -> None:
    """The publication command exposes every authorization identity explicitly."""

    script = Path(__file__).resolve().parents[1] / "scripts" / "agent_harness.py"
    completed = subprocess.run(
        (sys.executable, str(script), "publish", "--help"),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "--issue" in completed.stdout
    assert "--base" in completed.stdout
    assert "--sha" in completed.stdout
    assert "--branch" in completed.stdout


def test_publish_cli_success_output_is_compact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Successful CLI output excludes verbose publication evidence."""

    repository, base_sha, approved_sha = committed_publication_candidate(tmp_path)
    marker = "verbose-success-payload-that-must-not-reach-stdout"

    def successful_publish(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "ci": {"runId": 202, "jobs": [{"detail": marker * 1000}]},
            "receiptPath": ".agent/receipts/publication.json",
        }

    monkeypatch.setattr(agent_harness, "publish_repository", successful_publish)
    monkeypatch.setattr(
        agent_harness,
        "_github_repository_identity_from_url",
        lambda _url: "core-console/back-end",
    )
    exit_code = agent_harness.main(
        (
            "--repo",
            str(repository),
            "publish",
            "--issue",
            "15",
            "--base",
            base_sha,
            "--sha",
            approved_sha,
            "--branch",
            "main",
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert marker not in output
    assert output.startswith(f"PASS issue=15 sha={approved_sha} ci-run=202")


def test_github_commands_are_explicitly_bound_to_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ambient gh configuration cannot redirect CI or issue operations."""

    repository, _base_sha = initialized_repository(tmp_path)
    commands: list[tuple[str, ...]] = []

    def successful_command(
        command: tuple[str, ...],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, b"[]", b"")

    monkeypatch.setattr(subprocess, "run", successful_command)
    github = GitHubCli(
        repository,
        repository / ".agent" / "logs" / "publication-test",
        "core-console/back-end",
    )
    assert github.list_validation_runs("main") == []

    assert commands == [
        (
            "gh",
            "run",
            "list",
            "--repo",
            "core-console/back-end",
            "--workflow",
            "Validate",
            "--branch",
            "main",
            "--limit",
            "100",
            "--json",
            "databaseId,url,name,workflowName,status,conclusion,headSha,createdAt",
        )
    ]


def test_github_failure_output_is_retained_without_dumping_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Large GitHub diagnostics stay in a retained publication log."""

    repository, _base_sha = initialized_repository(tmp_path)
    marker = "large-github-failure-marker"

    def failed_command(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            args=("gh",),
            returncode=1,
            stdout=(f"{marker}\n" + "detail\n" * 5000).encode(),
            stderr=b"compact failure\n",
        )

    monkeypatch.setattr(subprocess, "run", failed_command)
    logs = repository / ".agent" / "logs" / "publication-test"
    github = GitHubCli(repository, logs, "core-console/back-end")

    with pytest.raises(PublicationError) as error:
        github.list_validation_runs("main")

    assert marker not in str(error.value)
    log = next(logs.glob("*.log")).read_text(encoding="utf-8")
    assert marker in log
    assert len(str(error.value)) < 300


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
