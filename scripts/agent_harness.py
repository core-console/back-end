"""Create deterministic, snapshot-bound repository agent evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from base64 import b64encode
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

SCRIPT_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_REPOSITORY_ROOT))

SNAPSHOT_SCHEMA = "core-console-agent-snapshot/v1"
OUTPUT_DIRECTORY = ".agent"
REVIEW_SCHEMA = "core-console-agent-review/v2"
VALIDATION_SCHEMA = "core-console-agent-validation/v1"
PREFLIGHT_SCHEMA = "core-console-agent-preflight/v1"
PUBLICATION_SCHEMA = "core-console-agent-publication/v1"


@dataclass(frozen=True)
class RepositorySnapshot:
    """Identity of committed, index, worktree, and untracked repository state."""

    base_sha: str
    head_sha: str
    digest: str


@dataclass(frozen=True)
class ChangedFile:
    """Classification of one path in the implementation delta."""

    path: str
    committed: bool
    staged: bool
    unstaged: bool
    untracked: bool


@dataclass(frozen=True)
class ReviewState:
    """Paths and classifications needed to review one exact snapshot."""

    snapshot: RepositorySnapshot
    changed_files: tuple[ChangedFile, ...]
    artifact_path: Path
    receipt_path: Path
    validation: ReviewValidationState

    @property
    def validation_status(self) -> str:
        """Retain the concise snapshot-identity classification."""

        return self.validation.snapshot_status


type ValidationSnapshotStatus = Literal["matching", "stale", "absent"]
type ValidationOutcome = Literal["PASS", "FAIL", "STALE"]


@dataclass(frozen=True)
class ReviewValidationState:
    """Separate validation identity from outcome and protected coverage."""

    snapshot_status: ValidationSnapshotStatus
    outcome: ValidationOutcome | None
    protected_requested: bool | None
    protected_exercised: bool | None
    receipt_path: str | None


class ReviewSnapshotStaleError(RuntimeError):
    """Raised when review evidence spans more than one implementation snapshot."""


class PublicationError(RuntimeError):
    """Raised when publication cannot proceed without weakening its contract."""


class GitHubBoundary(Protocol):
    """Narrow external boundary used by the publication workflow."""

    def repository_identity(self) -> str: ...

    def list_validation_runs(
        self,
        branch: str,
        *,
        timeout_seconds: float | None = None,
    ) -> Sequence[Mapping[str, object]]: ...

    def get_validation_run(
        self,
        run_id: int,
        *,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, object]: ...

    def get_issue(self, issue: int) -> Mapping[str, object]: ...

    def close_issue(self, issue: int) -> None: ...


class PublicationGitBoundary(Protocol):
    """Narrow logged boundary for publication-specific remote Git commands."""

    def live_branch_sha(self, remote_url: str, branch: str, *, phase: str) -> str: ...

    def push(self, remote_url: str, approved_sha: str, branch: str) -> None: ...


class PublicationGit:
    """Run publication remote Git commands with complete retained diagnostics."""

    def __init__(self, root: Path, log_directory: Path) -> None:
        self._root = root
        self._log_directory = log_directory
        self._command_index = 0

    def _run(self, phase: str, *arguments: str) -> bytes:
        self._command_index += 1
        command = ("git", *arguments)
        result = subprocess.run(
            command,
            cwd=self._root,
            check=False,
            capture_output=True,
        )
        log_path = self._log_directory / f"{self._command_index:02d}-{_safe_label(phase)}-git.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            (
                f"$ {subprocess.list2cmdline(command)}\n\n[stdout]\n"
                f"{result.stdout.decode('utf-8', errors='replace')}\n[stderr]\n"
                f"{result.stderr.decode('utf-8', errors='replace')}"
            ),
            encoding="utf-8",
        )
        if result.returncode != 0:
            raise PublicationError(
                f"publication Git phase={phase} failed; "
                f"detailed-log={log_path.relative_to(self._root).as_posix()}"
            )
        return result.stdout

    def live_branch_sha(self, remote_url: str, branch: str, *, phase: str) -> str:
        output = self._run(
            phase,
            "ls-remote",
            "--exit-code",
            remote_url,
            f"refs/heads/{branch}",
        )
        fields = _text(output).split()
        if len(fields) != 2 or fields[1] != f"refs/heads/{branch}":
            raise PublicationError(
                f"publication Git phase={phase} returned ambiguous branch evidence"
            )
        return fields[0]

    def push(self, remote_url: str, approved_sha: str, branch: str) -> None:
        self._run(
            "push",
            "push",
            remote_url,
            f"{approved_sha}:refs/heads/{branch}",
        )


class GitHubCli:
    """GitHub CLI adapter that retains verbose responses outside stdout."""

    def __init__(self, root: Path, log_directory: Path, repository: str) -> None:
        self._root = root
        self._log_directory = log_directory
        self._repository = repository
        self._command_index = 0

    def _run(self, *arguments: str, timeout_seconds: float | None = None) -> bytes:
        self._command_index += 1
        command = ("gh", *arguments)
        try:
            result = subprocess.run(
                command,
                cwd=self._root,
                check=False,
                capture_output=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            log_path = self._write_log(
                command,
                _command_output_bytes(error.stdout),
                _command_output_bytes(error.stderr),
            )
            raise PublicationError(
                "GitHub command timed out; "
                f"detailed-log={log_path.relative_to(self._root).as_posix()}"
            ) from error
        log_path = self._write_log(command, result.stdout, result.stderr)
        if result.returncode != 0:
            raise PublicationError(
                f"GitHub command failed; detailed-log={log_path.relative_to(self._root).as_posix()}"
            )
        return result.stdout

    def _write_log(self, command: tuple[str, ...], stdout: bytes, stderr: bytes) -> Path:
        """Persist one complete GitHub command transcript."""

        log_path = self._log_directory / f"{self._command_index:02d}-github.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            (
                f"$ {subprocess.list2cmdline(command)}\n\n[stdout]\n"
                f"{stdout.decode('utf-8', errors='replace')}\n[stderr]\n"
                f"{stderr.decode('utf-8', errors='replace')}"
            ),
            encoding="utf-8",
        )
        return log_path

    def _json(self, *arguments: str, timeout_seconds: float | None = None) -> object:
        try:
            return json.loads(self._run(*arguments, timeout_seconds=timeout_seconds))
        except json.JSONDecodeError as error:
            raise PublicationError("GitHub returned invalid JSON; see publication logs") from error

    def repository_identity(self) -> str:
        return self._repository

    def list_validation_runs(
        self,
        branch: str,
        *,
        timeout_seconds: float | None = None,
    ) -> Sequence[Mapping[str, object]]:
        data = self._json(
            "run",
            "list",
            "--repo",
            self._repository,
            "--workflow",
            "Validate",
            "--branch",
            branch,
            "--limit",
            "100",
            "--json",
            "databaseId,url,name,workflowName,status,conclusion,headSha,createdAt",
            timeout_seconds=timeout_seconds,
        )
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise PublicationError("GitHub validation run list is unavailable")
        return data

    def get_validation_run(
        self,
        run_id: int,
        *,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, object]:
        data = self._json(
            "run",
            "view",
            str(run_id),
            "--repo",
            self._repository,
            "--json",
            "databaseId,url,name,workflowName,status,conclusion,headSha,jobs",
            timeout_seconds=timeout_seconds,
        )
        if not isinstance(data, dict):
            raise PublicationError("GitHub validation run is unavailable")
        return data

    def get_issue(self, issue: int) -> Mapping[str, object]:
        data = self._json(
            "issue",
            "view",
            str(issue),
            "--repo",
            self._repository,
            "--json",
            "number,state",
        )
        if not isinstance(data, dict):
            raise PublicationError("GitHub issue state is unavailable")
        return data

    def close_issue(self, issue: int) -> None:
        self._run(
            "issue",
            "close",
            str(issue),
            "--repo",
            self._repository,
            "--reason",
            "completed",
        )


def _command_output_bytes(output: bytes | str | None) -> bytes:
    """Normalize subprocess timeout output for lossless retained diagnostics."""

    if output is None:
        return b""
    if isinstance(output, bytes):
        return output
    return output.encode("utf-8", errors="replace")


@dataclass(frozen=True)
class ValidationCheck:
    """One ordered repository quality gate."""

    label: str
    command: tuple[str, ...]


@dataclass(frozen=True)
class ValidationCheckResult:
    """Compact result and detailed-log location for one quality gate."""

    label: str
    status: str
    return_code: int
    log_path: Path


@dataclass(frozen=True)
class ValidationResult:
    """Snapshot-bound result of an ordered validation run."""

    outcome: str
    snapshot_before: RepositorySnapshot
    snapshot_after: RepositorySnapshot
    checks: tuple[ValidationCheckResult, ...]
    receipt_path: Path
    failed_check: str | None
    test_summary: str
    protected_postgresql_exercised: bool


def _git(repository: Path, *arguments: str) -> bytes:
    """Return exact Git output or raise a focused command failure."""

    result = subprocess.run(
        ("git", *arguments),
        cwd=repository,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(arguments)} failed: {message}")
    return result.stdout


def _text(output: bytes) -> str:
    """Decode Git identifiers and paths without losing unusual byte values."""

    return output.decode("utf-8", errors="surrogateescape").strip()


def _record(digest: hashlib._Hash, label: bytes, value: bytes) -> None:
    """Add an unambiguous length-prefixed record to a digest."""

    digest.update(len(label).to_bytes(8, "big"))
    digest.update(label)
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _paths(output: bytes) -> tuple[str, ...]:
    """Decode a NUL-delimited Git path list deterministically."""

    return tuple(
        os.fsdecode(path)
        for path in output.split(b"\0")
        if path and not os.fsdecode(path).startswith(f"{OUTPUT_DIRECTORY}/")
    )


def snapshot_repository(repository: Path, base: str) -> RepositorySnapshot:
    """Hash the complete implementation state relative to an explicit base."""

    root = Path(_text(_git(repository, "rev-parse", "--show-toplevel")))
    base_sha = _text(_git(root, "rev-parse", f"{base}^{{commit}}"))
    head_sha = _text(_git(root, "rev-parse", "HEAD"))

    digest = hashlib.sha256()
    _record(digest, b"schema", SNAPSHOT_SCHEMA.encode())
    _record(digest, b"base", base_sha.encode())
    _record(digest, b"head", head_sha.encode())
    _record(
        digest,
        b"committed-diff",
        _git(root, "diff", "--raw", "--no-abbrev", "-z", base_sha, head_sha),
    )
    _record(digest, b"index", _git(root, "ls-files", "--stage", "-z"))
    _record(
        digest,
        b"staged-diff",
        _git(root, "diff", "--cached", "--raw", "--no-abbrev", "-z", head_sha),
    )
    _record(
        digest,
        b"unstaged-diff",
        _git(root, "diff", "--raw", "--no-abbrev", "-z"),
    )

    tracked_paths = set(_paths(_git(root, "ls-files", "-z")))
    tracked_paths.update(_paths(_git(root, "ls-tree", "-r", "--name-only", "-z", head_sha)))
    for relative_path in sorted(tracked_paths):
        path = root / relative_path
        _record(digest, b"tracked-path", os.fsencode(relative_path))
        if path.is_file() or path.is_symlink():
            _record(digest, b"tracked-content", path.read_bytes())
        else:
            _record(digest, b"tracked-missing", b"")

    for relative_path in sorted(
        _paths(_git(root, "ls-files", "--others", "--exclude-standard", "-z"))
    ):
        path = root / relative_path
        _record(digest, b"untracked-path", os.fsencode(relative_path))
        _record(digest, b"untracked-content", path.read_bytes())

    return RepositorySnapshot(
        base_sha=base_sha,
        head_sha=head_sha,
        digest=digest.hexdigest(),
    )


def _changed_files(root: Path, base_sha: str, head_sha: str) -> tuple[ChangedFile, ...]:
    """Return a stable union of committed, staged, unstaged, and untracked paths."""

    committed = set(_paths(_git(root, "diff", "--name-only", "-z", base_sha, head_sha)))
    staged = set(_paths(_git(root, "diff", "--cached", "--name-only", "-z", head_sha)))
    unstaged = set(_paths(_git(root, "diff", "--name-only", "-z")))
    untracked = set(_paths(_git(root, "ls-files", "--others", "--exclude-standard", "-z")))
    return tuple(
        ChangedFile(
            path=path,
            committed=path in committed,
            staged=path in staged,
            unstaged=path in unstaged,
            untracked=path in untracked,
        )
        for path in sorted(committed | staged | unstaged | untracked)
    )


def _changed_file_data(changed_files: Sequence[ChangedFile]) -> list[dict[str, object]]:
    """Serialize path classifications consistently across receipts."""

    return [
        {
            "path": item.path,
            "committed": item.committed,
            "staged": item.staged,
            "unstaged": item.unstaged,
            "untracked": item.untracked,
        }
        for item in changed_files
    ]


def _patch(root: Path, *arguments: str) -> str:
    """Render a canonical full-index binary-capable Git patch."""

    return _git(
        root,
        "diff",
        "--binary",
        "--full-index",
        "--no-ext-diff",
        *arguments,
    ).decode("utf-8", errors="replace")


def _untracked_artifact(root: Path, path: str) -> dict[str, object]:
    """Represent an untracked file as readable text or lossless base64."""

    content = (root / path).read_bytes()
    identity = hashlib.sha256(content).hexdigest()
    try:
        text_content = content.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "path": path,
            "rendering": "base64",
            "sha256": identity,
            "sizeBytes": len(content),
            "contentBase64": b64encode(content).decode("ascii"),
        }
    if "\0" in text_content:
        return {
            "path": path,
            "rendering": "base64",
            "sha256": identity,
            "sizeBytes": len(content),
            "contentBase64": b64encode(content).decode("ascii"),
        }
    return {
        "path": path,
        "rendering": "utf-8",
        "sha256": identity,
        "sizeBytes": len(content),
        "content": text_content,
    }


def _review_validation_data(validation: ReviewValidationState) -> dict[str, object]:
    """Serialize review validation evidence for receipts and compact CLI output."""

    return {
        "snapshotStatus": validation.snapshot_status,
        "outcome": validation.outcome,
        "protectedPostgresql": {
            "requested": validation.protected_requested,
            "exercised": validation.protected_exercised,
        },
        "receiptPath": validation.receipt_path,
    }


def _validation_binding(
    root: Path,
    snapshot: RepositorySnapshot,
) -> ReviewValidationState:
    """Classify available validation evidence for the current snapshot."""

    receipts = root / OUTPUT_DIRECTORY / "receipts"
    current = receipts / f"validation-{snapshot.digest}.json"
    if current.is_file():
        try:
            data = json.loads(current.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            data = None
        identity_fields: dict[str, object] = {
            "schema": VALIDATION_SCHEMA,
            "baseSha": snapshot.base_sha,
            "headSha": snapshot.head_sha,
            "snapshotDigest": snapshot.digest,
        }
        if isinstance(data, dict) and all(
            data.get(key) == value for key, value in identity_fields.items()
        ):
            outcome = data.get("outcome")
            protected = data.get("protectedPostgresql")
            if (
                outcome in {"PASS", "FAIL", "STALE"}
                and isinstance(protected, dict)
                and isinstance(protected.get("requested"), bool)
                and isinstance(protected.get("exercised"), bool)
            ):
                snapshot_status: ValidationSnapshotStatus = "stale"
                if (
                    data.get("snapshotAfterDigest") == snapshot.digest
                    and data.get("snapshotUnchanged") is True
                ):
                    snapshot_status = "matching"
                return ReviewValidationState(
                    snapshot_status=snapshot_status,
                    outcome=outcome,
                    protected_requested=protected["requested"],
                    protected_exercised=protected["exercised"],
                    receipt_path=current.relative_to(root).as_posix(),
                )
        return ReviewValidationState("stale", None, None, None, None)
    if receipts.is_dir() and any(receipts.glob("validation-*.json")):
        return ReviewValidationState("stale", None, None, None, None)
    return ReviewValidationState("absent", None, None, None, None)


def create_review_state(repository: Path, base: str) -> ReviewState:
    """Write complete worktree-aware review evidence for one snapshot."""

    root = Path(_text(_git(repository, "rev-parse", "--show-toplevel")))
    snapshot = snapshot_repository(root, base)
    changed_files = _changed_files(root, snapshot.base_sha, snapshot.head_sha)
    untracked_paths = [item.path for item in changed_files if item.untracked]
    output_root = root / OUTPUT_DIRECTORY
    artifact_path = output_root / "reviews" / f"{snapshot.digest}.json"
    receipt_path = output_root / "receipts" / f"review-{snapshot.digest}.json"

    artifact: dict[str, object] = {
        "schema": REVIEW_SCHEMA,
        "baseSha": snapshot.base_sha,
        "headSha": snapshot.head_sha,
        "snapshotDigest": snapshot.digest,
        "committedPatch": _patch(root, snapshot.base_sha, snapshot.head_sha),
        "stagedPatch": _patch(root, "--cached", snapshot.head_sha),
        "unstagedPatch": _patch(root),
        "untrackedFiles": [_untracked_artifact(root, path) for path in untracked_paths],
    }
    snapshot_after_evidence = snapshot_repository(root, snapshot.base_sha)
    if snapshot_after_evidence.digest != snapshot.digest:
        raise ReviewSnapshotStaleError(
            "implementation changed while review evidence was assembled; retry review-state"
        )

    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        f"{json.dumps(artifact, indent=2, sort_keys=True, ensure_ascii=False)}\n",
        encoding="utf-8",
    )
    validation = _validation_binding(root, snapshot)
    receipt: dict[str, object] = {
        "schema": REVIEW_SCHEMA,
        "baseSha": snapshot.base_sha,
        "headSha": snapshot.head_sha,
        "snapshotDigest": snapshot.digest,
        "changedFiles": _changed_file_data(changed_files),
        "artifactPath": artifact_path.relative_to(root).as_posix(),
        "validation": _review_validation_data(validation),
    }
    receipt_path.write_text(
        f"{json.dumps(receipt, indent=2, sort_keys=True)}\n",
        encoding="utf-8",
    )
    return ReviewState(
        snapshot=snapshot,
        changed_files=changed_files,
        artifact_path=artifact_path,
        receipt_path=receipt_path,
        validation=validation,
    )


def _validation_checks(
    base_sha: str,
    head_sha: str,
    authority_checks: Sequence[ValidationCheck] | None,
) -> tuple[ValidationCheck, ...]:
    """Extend the existing validation authority with worktree whitespace gates."""

    if authority_checks is None:
        from scripts.validate import CHECKS

        checks = tuple(
            ValidationCheck(label=label, command=tuple(command)) for label, command in CHECKS
        )
    else:
        checks = tuple(authority_checks)
    return (
        *checks,
        ValidationCheck(
            "base to HEAD git diff --check",
            ("git", "diff", "--check", base_sha, head_sha),
        ),
        ValidationCheck("git diff --check", ("git", "diff", "--check")),
        ValidationCheck(
            "staged git diff --check",
            ("git", "diff", "--cached", "--check"),
        ),
    )


def _write_check_log(
    path: Path,
    check: ValidationCheck,
    stdout: bytes,
    stderr: bytes,
) -> None:
    """Retain complete quality-gate output outside normal agent context."""

    path.parent.mkdir(parents=True, exist_ok=True)
    command = subprocess.list2cmdline(check.command)
    content = (
        f"$ {command}\n\n[stdout]\n"
        f"{stdout.decode('utf-8', errors='replace')}\n[stderr]\n"
        f"{stderr.decode('utf-8', errors='replace')}"
    )
    path.write_text(content, encoding="utf-8")


def _safe_label(label: str) -> str:
    """Create a stable readable log filename from a check label."""

    return re.sub(r"[^a-z0-9]+", "-", label.casefold()).strip("-")


def _test_summary(checks: Sequence[ValidationCheckResult]) -> str:
    """Extract only pytest's useful terminal count from its retained log."""

    pytest_results = [result for result in checks if result.label == "pytest"]
    if not pytest_results:
        return "not run"
    output = pytest_results[-1].log_path.read_text(encoding="utf-8")
    matches = re.findall(r"(?m)(\d+ passed(?:, \d+ (?:failed|skipped|xfailed|xpassed))*)", output)
    return matches[-1] if matches else "see log"


def _write_validation_receipt(
    root: Path,
    result: ValidationResult,
    protected_requested: bool,
) -> None:
    """Persist a versioned machine-readable validation result."""

    receipt: dict[str, object] = {
        "schema": VALIDATION_SCHEMA,
        "outcome": result.outcome,
        "baseSha": result.snapshot_before.base_sha,
        "headSha": result.snapshot_before.head_sha,
        "snapshotDigest": result.snapshot_before.digest,
        "snapshotAfterDigest": result.snapshot_after.digest,
        "snapshotUnchanged": (result.snapshot_before.digest == result.snapshot_after.digest),
        "failedCheck": result.failed_check,
        "testSummary": result.test_summary,
        "protectedPostgresql": {
            "requested": protected_requested,
            "exercised": result.protected_postgresql_exercised,
        },
        "checks": [
            {
                "label": check.label,
                "status": check.status,
                "returnCode": check.return_code,
                "logPath": check.log_path.relative_to(root).as_posix(),
            }
            for check in result.checks
        ],
    }
    result.receipt_path.parent.mkdir(parents=True, exist_ok=True)
    result.receipt_path.write_text(
        f"{json.dumps(receipt, indent=2, sort_keys=True)}\n",
        encoding="utf-8",
    )


def validate_repository(
    repository: Path,
    base: str,
    *,
    protected: bool,
    checks: Sequence[ValidationCheck] | None = None,
    environment: Mapping[str, str] | None = None,
) -> ValidationResult:
    """Run ordered gates and issue PASS only for an unchanged snapshot."""

    root = Path(_text(_git(repository, "rev-parse", "--show-toplevel")))
    snapshot_before = snapshot_repository(root, base)
    receipt_path = (
        root / OUTPUT_DIRECTORY / "receipts" / f"validation-{snapshot_before.digest}.json"
    )
    command_environment = dict(os.environ if environment is None else environment)
    check_results: list[ValidationCheckResult] = []

    if protected and not command_environment.get("TEST_DATABASE_URL"):
        snapshot_after = snapshot_repository(root, base)
        result = ValidationResult(
            outcome="FAIL",
            snapshot_before=snapshot_before,
            snapshot_after=snapshot_after,
            checks=(),
            receipt_path=receipt_path,
            failed_check="protected PostgreSQL preflight",
            test_summary="not run",
            protected_postgresql_exercised=False,
        )
        _write_validation_receipt(root, result, protected)
        return result

    failed_check: str | None = None
    selected_checks = _validation_checks(
        snapshot_before.base_sha,
        snapshot_before.head_sha,
        checks,
    )
    for index, check in enumerate(selected_checks, start=1):
        check_environment = command_environment.copy()
        if protected and check.label == "pytest":
            check_environment["CI"] = "true"
        completed = subprocess.run(
            check.command,
            cwd=root,
            check=False,
            capture_output=True,
            env=check_environment,
        )
        log_path = (
            root
            / OUTPUT_DIRECTORY
            / "logs"
            / snapshot_before.digest
            / f"{index:02d}-{_safe_label(check.label)}.log"
        )
        _write_check_log(log_path, check, completed.stdout, completed.stderr)
        status = "PASS" if completed.returncode == 0 else "FAIL"
        check_results.append(
            ValidationCheckResult(
                label=check.label,
                status=status,
                return_code=completed.returncode,
                log_path=log_path,
            )
        )
        if completed.returncode != 0:
            failed_check = check.label
            break

    snapshot_after = snapshot_repository(root, base)
    unchanged = snapshot_before.digest == snapshot_after.digest
    outcome = "PASS" if failed_check is None and unchanged else "FAIL"
    if not unchanged:
        outcome = "STALE"
        failed_check = "implementation snapshot changed during validation"
    protected_exercised = protected and any(
        result.label == "pytest" and result.status == "PASS" for result in check_results
    )
    result = ValidationResult(
        outcome=outcome,
        snapshot_before=snapshot_before,
        snapshot_after=snapshot_after,
        checks=tuple(check_results),
        receipt_path=receipt_path,
        failed_check=failed_check,
        test_summary=_test_summary(check_results),
        protected_postgresql_exercised=protected_exercised,
    )
    _write_validation_receipt(root, result, protected)
    return result


def format_validation_result(result: ValidationResult) -> str:
    """Return compressed success facts or a focused failure pointer."""

    protected = "exercised" if result.protected_postgresql_exercised else "not exercised"
    checks = ", ".join(f"{check.label}={check.status}" for check in result.checks)
    if result.outcome == "PASS":
        return (
            f"PASS snapshot={result.snapshot_before.digest} "
            f"base={result.snapshot_before.base_sha} HEAD={result.snapshot_before.head_sha} "
            f"tests={result.test_summary} protected-postgresql={protected} "
            f"checks=[{checks}] receipt={result.receipt_path}"
        )
    return (
        f"{result.outcome} check={result.failed_check} "
        f"snapshot={result.snapshot_before.digest} receipt={result.receipt_path}"
    )


def collect_preflight(repository: Path, base: str) -> dict[str, object]:
    """Write and return compact local-only repository preflight facts."""

    root = Path(_text(_git(repository, "rev-parse", "--show-toplevel")))
    snapshot = snapshot_repository(root, base)
    changed_files = _changed_files(root, snapshot.base_sha, snapshot.head_sha)
    branch = _text(_git(root, "branch", "--show-current")) or None
    origin_url_result = subprocess.run(
        ("git", "remote", "get-url", "origin"),
        cwd=root,
        check=False,
        capture_output=True,
    )
    origin_url = _text(origin_url_result.stdout) if origin_url_result.returncode == 0 else None
    origin_main_result = subprocess.run(
        ("git", "rev-parse", "--verify", "refs/remotes/origin/main"),
        cwd=root,
        check=False,
        capture_output=True,
    )
    origin_main_sha: str | None = None
    ahead: int | None = None
    behind: int | None = None
    if origin_main_result.returncode == 0:
        origin_main_sha = _text(origin_main_result.stdout)
        counts = _text(
            _git(
                root,
                "rev-list",
                "--left-right",
                "--count",
                f"{origin_main_sha}...{snapshot.head_sha}",
            )
        ).split()
        behind, ahead = (int(counts[0]), int(counts[1]))

    receipt_path = root / OUTPUT_DIRECTORY / "receipts" / f"preflight-{snapshot.digest}.json"
    receipt: dict[str, object] = {
        "schema": PREFLIGHT_SCHEMA,
        "repository": {
            "rootName": root.name,
            "originUrl": origin_url,
        },
        "branch": branch,
        "headSha": snapshot.head_sha,
        "localOriginMainSha": origin_main_sha,
        "ahead": ahead,
        "behind": behind,
        "dirty": bool(changed_files),
        "changedFiles": _changed_file_data(changed_files),
        "snapshotDigest": snapshot.digest,
        "baseSha": snapshot.base_sha,
        "remoteTrackingState": "local; no implicit fetch",
        "receiptPath": receipt_path.relative_to(root).as_posix(),
    }
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(
        f"{json.dumps(receipt, indent=2, sort_keys=True)}\n",
        encoding="utf-8",
    )
    return receipt


def publish_repository(
    repository: Path,
    *,
    issue: int,
    base: str,
    approved_sha: str,
    branch: str,
    github: GitHubBoundary,
    publication_git: PublicationGitBoundary | None = None,
    ci_timeout_seconds: float = 1200,
    ci_poll_seconds: float = 10,
) -> dict[str, object]:
    """Publish one explicitly approved commit after deterministic safety gates."""

    root = Path(_text(_git(repository, "rev-parse", "--show-toplevel")))
    publication_log_directory = (
        root / OUTPUT_DIRECTORY / "logs" / "publication" / f"issue-{issue}-{approved_sha}"
    )
    if publication_git is None:
        publication_git = PublicationGit(root, publication_log_directory)
    base_sha = _text(_git(root, "rev-parse", f"{base}^{{commit}}"))
    head_sha = _text(_git(root, "rev-parse", "HEAD"))
    if head_sha != approved_sha:
        raise PublicationError("HEAD does not equal approved SHA")
    current_branch = _text(_git(root, "branch", "--show-current"))
    if current_branch != branch:
        raise PublicationError("current branch does not equal expected branch")
    parent_sha = _text(_git(root, "rev-parse", "HEAD^"))
    if parent_sha != base_sha:
        raise PublicationError("approved HEAD is not the single child of the expected base")
    if _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all"):
        raise PublicationError("worktree is not clean")
    local_remote_sha = _text(_git(root, "rev-parse", "--verify", f"refs/remotes/origin/{branch}"))
    if local_remote_sha not in {base_sha, approved_sha}:
        raise PublicationError("local origin tracking state is behind or diverged")
    behind_text, ahead_text = _text(
        _git(
            root,
            "rev-list",
            "--left-right",
            "--count",
            f"{local_remote_sha}...{approved_sha}",
        )
    ).split()
    expected_ahead = 1 if local_remote_sha == base_sha else 0
    if int(behind_text) != 0 or int(ahead_text) != expected_ahead:
        raise PublicationError("local origin tracking state is behind or diverged")

    snapshot = snapshot_repository(root, base_sha)
    validation_path = root / OUTPUT_DIRECTORY / "receipts" / f"validation-{snapshot.digest}.json"
    validation = _read_json_object(validation_path, "validation receipt is stale")
    validation_identity = {
        "schema": VALIDATION_SCHEMA,
        "baseSha": base_sha,
        "headSha": approved_sha,
        "snapshotDigest": snapshot.digest,
        "snapshotAfterDigest": snapshot.digest,
        "snapshotUnchanged": True,
    }
    if any(validation.get(key) != value for key, value in validation_identity.items()):
        raise PublicationError("validation receipt is stale")
    if validation.get("outcome") != "PASS":
        raise PublicationError("validation outcome is not PASS")
    protected = validation.get("protectedPostgresql")
    if not isinstance(protected, dict) or not (
        protected.get("requested") is True and protected.get("exercised") is True
    ):
        raise PublicationError("protected PostgreSQL evidence is required")

    review_path = root / OUTPUT_DIRECTORY / "receipts" / f"review-{snapshot.digest}.json"
    review = _read_json_object(review_path, "review-state receipt is stale")
    if any(
        review.get(key) != value
        for key, value in {
            "schema": REVIEW_SCHEMA,
            "baseSha": base_sha,
            "headSha": approved_sha,
            "snapshotDigest": snapshot.digest,
        }.items()
    ):
        raise PublicationError("review-state receipt is stale")
    review_validation = review.get("validation")
    if not isinstance(review_validation, dict) or review_validation != {
        "snapshotStatus": "matching",
        "outcome": "PASS",
        "protectedPostgresql": {"requested": True, "exercised": True},
        "receiptPath": validation_path.relative_to(root).as_posix(),
    }:
        raise PublicationError("review-state receipt is stale")
    artifact_relative = review.get("artifactPath")
    if not isinstance(artifact_relative, str):
        raise PublicationError("review-state receipt is stale")
    artifact = _read_json_object(root / artifact_relative, "review-state artifact is stale")
    if any(
        artifact.get(key) != value
        for key, value in {
            "schema": REVIEW_SCHEMA,
            "baseSha": base_sha,
            "headSha": approved_sha,
            "snapshotDigest": snapshot.digest,
        }.items()
    ):
        raise PublicationError("review-state artifact is stale")
    fetch_url, push_url, origin_identity = _publication_destinations(root)
    live_remote_sha = publication_git.live_branch_sha(
        fetch_url,
        branch,
        phase="before-push-live-remote",
    )
    if live_remote_sha not in {base_sha, approved_sha}:
        raise PublicationError("live remote branch drifted from expected base")
    repository_identity = github.repository_identity()
    if repository_identity != origin_identity:
        raise PublicationError("GitHub repository does not match origin")
    remote_before_push = live_remote_sha
    push_performed = False
    if live_remote_sha == base_sha:
        publication_git.push(push_url, approved_sha, branch)
        push_performed = True
    remote_after_push = publication_git.live_branch_sha(
        fetch_url,
        branch,
        phase="after-push-live-remote",
    )
    if remote_after_push != approved_sha:
        raise PublicationError("live remote does not equal approved SHA after push")

    try:
        run = _wait_for_exact_validation_run(
            github,
            branch=branch,
            approved_sha=approved_sha,
            timeout_seconds=ci_timeout_seconds,
            poll_seconds=ci_poll_seconds,
        )
    except PublicationError as error:
        receipt_path = _write_ci_failure_receipt(
            root,
            publication_git=publication_git,
            fetch_url=fetch_url,
            repository_identity=repository_identity,
            issue=issue,
            branch=branch,
            base_sha=base_sha,
            approved_sha=approved_sha,
            snapshot_digest=snapshot.digest,
            validation_path=validation_path,
            review_path=review_path,
            remote_before_push=remote_before_push,
            remote_after_push=remote_after_push,
            push_performed=push_performed,
            run=None,
            failure=str(error),
        )
        raise PublicationError(
            f"{error}; receipt={receipt_path.relative_to(root).as_posix()}"
        ) from error
    run_id = run.get("databaseId")
    jobs = run.get("jobs")
    if not isinstance(run_id, int) or not isinstance(jobs, list):
        raise PublicationError("exact-SHA CI validated evidence is internally inconsistent")
    conclusion = run.get("conclusion")
    if conclusion != "success":
        receipt_path = _write_ci_failure_receipt(
            root,
            publication_git=publication_git,
            fetch_url=fetch_url,
            repository_identity=repository_identity,
            issue=issue,
            branch=branch,
            base_sha=base_sha,
            approved_sha=approved_sha,
            snapshot_digest=snapshot.digest,
            validation_path=validation_path,
            review_path=review_path,
            remote_before_push=remote_before_push,
            remote_after_push=remote_after_push,
            push_performed=push_performed,
            run=run,
            failure=f"exact-SHA CI concluded {conclusion}",
        )
        raise PublicationError(
            f"exact-SHA CI concluded {conclusion}; "
            f"receipt={receipt_path.relative_to(root).as_posix()}"
        )
    if (
        publication_git.live_branch_sha(
            fetch_url,
            branch,
            phase="after-ci-live-remote",
        )
        != approved_sha
    ):
        raise PublicationError("live remote changed after exact-SHA CI")

    issue_data = github.get_issue(issue)
    if issue_data.get("number") != issue:
        raise PublicationError("GitHub returned a different issue")
    issue_state = issue_data.get("state")
    if issue_state == "OPEN":
        if (
            publication_git.live_branch_sha(
                fetch_url,
                branch,
                phase="before-close-live-remote",
            )
            != approved_sha
        ):
            raise PublicationError("live remote changed immediately before issue closure")
        github.close_issue(issue)
        issue_data = github.get_issue(issue)
        if issue_data.get("number") != issue or issue_data.get("state") != "CLOSED":
            raise PublicationError("issue closure could not be verified")
    elif issue_state != "CLOSED":
        raise PublicationError("target issue state is not OPEN or CLOSED")

    final_head = _text(_git(root, "rev-parse", "HEAD"))
    final_remote = publication_git.live_branch_sha(
        fetch_url,
        branch,
        phase="final-live-remote",
    )
    clean = not bool(_git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all"))
    if final_head != approved_sha or final_remote != approved_sha or not clean:
        raise PublicationError("final repository state changed during publication")
    result = _publication_receipt_envelope(
        root,
        repository_identity=repository_identity,
        issue=issue,
        base_sha=base_sha,
        approved_sha=approved_sha,
        snapshot_digest=snapshot.digest,
        validation_path=validation_path,
        review_path=review_path,
        remote_before_push=remote_before_push,
        remote_after_push=remote_after_push,
        push_performed=push_performed,
        final_head=final_head,
        final_remote=final_remote,
        working_tree_clean=clean,
    )
    result.update(
        {
            "outcome": "PASS",
            "ci": {
                "runId": run_id,
                "url": run.get("url"),
                "workflowName": run.get("workflowName"),
                "name": run.get("name"),
                "headSha": run.get("headSha"),
                "jobs": jobs,
                "overallConclusion": conclusion,
            },
            "finalIssueState": issue_data.get("state"),
        }
    )
    receipt_path = _publication_receipt_path(root, issue, approved_sha)
    result["receiptPath"] = receipt_path.relative_to(root).as_posix()
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(
        f"{json.dumps(result, indent=2, sort_keys=True)}\n",
        encoding="utf-8",
    )
    return result


def _read_json_object(path: Path, failure: str) -> dict[str, object]:
    """Read one harness JSON object or fail with a compact publication error."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PublicationError(failure) from error
    if not isinstance(value, dict):
        raise PublicationError(failure)
    return value


def _publication_receipt_path(root: Path, issue: int, approved_sha: str) -> Path:
    """Return the stable idempotent receipt path for one authorized publication."""

    return root / OUTPUT_DIRECTORY / "receipts" / f"publication-issue-{issue}-{approved_sha}.json"


def _github_repository_identity_from_url(origin_url: str) -> str | None:
    """Extract owner/name from supported github.com origin URL forms."""

    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
        r"(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?",
        origin_url,
    )
    if match is None:
        return None
    return f"{match.group('owner')}/{match.group('name')}"


def _git_config_values(root: Path, key: str) -> tuple[str, ...]:
    """Return every configured value without applying Git URL rewrites."""

    result = subprocess.run(
        ("git", "config", "--get-all", key),
        cwd=root,
        check=False,
        capture_output=True,
    )
    if result.returncode == 1:
        return ()
    if result.returncode != 0:
        raise PublicationError(f"cannot resolve publication Git configuration for {key}")
    return tuple(line for line in _text(result.stdout).splitlines() if line)


def _effective_origin_push_urls(root: Path) -> tuple[str, ...]:
    """Ask Git to resolve every push URL after its configured URL rewriting."""

    result = subprocess.run(
        ("git", "remote", "get-url", "--push", "--all", "origin"),
        cwd=root,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise PublicationError("cannot resolve the effective origin push destination")
    return tuple(line for line in _text(result.stdout).splitlines() if line)


def _rewrite_stable_push_url(root: Path, push_url: str) -> str:
    """Require Git to leave the verified URL unchanged when used for another push."""

    instead_of_result = subprocess.run(
        ("git", "ls-remote", "--get-url", push_url),
        cwd=root,
        check=False,
        capture_output=True,
    )
    if instead_of_result.returncode != 0 or _text(instead_of_result.stdout) != push_url:
        raise PublicationError("effective push destination is not rewrite-stable")

    push_instead_of_result = subprocess.run(
        ("git", "config", "--get-regexp", r"^url\..*\.pushinsteadof$"),
        cwd=root,
        check=False,
        capture_output=True,
    )
    if push_instead_of_result.returncode == 0:
        raise PublicationError("effective push destination is not rewrite-stable")
    if push_instead_of_result.returncode != 1:
        raise PublicationError("effective push destination is not rewrite-stable")
    return push_url


def _publication_destinations(root: Path) -> tuple[str, str, str]:
    """Resolve one GitHub fetch URL and one identity-matching push destination."""

    fetch_urls = _git_config_values(root, "remote.origin.url")
    if len(fetch_urls) != 1:
        raise PublicationError("origin must have exactly one fetch destination")
    fetch_url = fetch_urls[0]
    repository_identity = _github_repository_identity_from_url(fetch_url)
    if repository_identity is None:
        raise PublicationError("origin fetch destination is not a supported GitHub URL")

    configured_push_urls = _git_config_values(root, "remote.origin.pushurl")
    push_urls = configured_push_urls or (fetch_url,)
    if len(push_urls) != 1:
        raise PublicationError("origin must have exactly one effective push destination")
    configured_push_url = push_urls[0]
    configured_push_identity = _github_repository_identity_from_url(configured_push_url)
    if configured_push_identity is None:
        raise PublicationError("origin push destination is not a supported GitHub URL")
    if configured_push_identity != repository_identity:
        raise PublicationError("origin push destination does not match fetch repository")

    effective_push_urls = _effective_origin_push_urls(root)
    if len(effective_push_urls) != 1:
        raise PublicationError("origin must have exactly one effective push destination")
    effective_push_url = effective_push_urls[0]
    effective_push_identity = _github_repository_identity_from_url(effective_push_url)
    if effective_push_identity is None:
        raise PublicationError("effective push destination is not a supported GitHub URL")
    if effective_push_identity != repository_identity:
        raise PublicationError("effective push destination does not match fetch repository")
    return fetch_url, _rewrite_stable_push_url(root, effective_push_url), repository_identity


def _publication_receipt_envelope(
    root: Path,
    *,
    repository_identity: str,
    issue: int,
    base_sha: str,
    approved_sha: str,
    snapshot_digest: str,
    validation_path: Path,
    review_path: Path,
    remote_before_push: str,
    remote_after_push: str,
    push_performed: bool,
    final_head: str,
    final_remote: str,
    working_tree_clean: bool,
) -> dict[str, object]:
    """Build fields shared by every publication outcome."""

    return {
        "schema": PUBLICATION_SCHEMA,
        "repository": repository_identity,
        "issueNumber": issue,
        "baseSha": base_sha,
        "approvedSha": approved_sha,
        "pushedSha": approved_sha,
        "validationReceipt": {
            "schema": VALIDATION_SCHEMA,
            "path": validation_path.relative_to(root).as_posix(),
            "snapshotDigest": snapshot_digest,
        },
        "reviewReceipt": {
            "schema": REVIEW_SCHEMA,
            "path": review_path.relative_to(root).as_posix(),
            "snapshotDigest": snapshot_digest,
        },
        "push": {"performed": push_performed, "mode": "normal-fast-forward"},
        "remoteBranchBeforePush": remote_before_push,
        "remoteBranchAfterPush": remote_after_push,
        "finalLocalHead": final_head,
        "finalRemoteSha": final_remote,
        "workingTreeClean": working_tree_clean,
    }


def _write_ci_failure_receipt(
    root: Path,
    *,
    publication_git: PublicationGitBoundary,
    fetch_url: str,
    repository_identity: str,
    issue: int,
    branch: str,
    base_sha: str,
    approved_sha: str,
    snapshot_digest: str,
    validation_path: Path,
    review_path: Path,
    remote_before_push: str,
    remote_after_push: str,
    push_performed: bool,
    run: Mapping[str, object] | None,
    failure: str,
) -> Path:
    """Retain compact exact-SHA CI failure metadata without closing an issue."""

    receipt_path = _publication_receipt_path(root, issue, approved_sha)
    ci: dict[str, object] = {
        "runId": None,
        "url": None,
        "workflowName": "Validate",
        "name": None,
        "headSha": approved_sha,
        "jobs": [],
        "overallConclusion": None,
    }
    if run is not None:
        ci.update(
            {
                "runId": run.get("databaseId"),
                "url": run.get("url"),
                "workflowName": run.get("workflowName"),
                "name": run.get("name"),
                "headSha": run.get("headSha"),
                "jobs": run.get("jobs", []),
                "overallConclusion": run.get("conclusion"),
            }
        )
    clean = not bool(_git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all"))
    receipt = _publication_receipt_envelope(
        root,
        repository_identity=repository_identity,
        issue=issue,
        base_sha=base_sha,
        approved_sha=approved_sha,
        snapshot_digest=snapshot_digest,
        validation_path=validation_path,
        review_path=review_path,
        remote_before_push=remote_before_push,
        remote_after_push=remote_after_push,
        push_performed=push_performed,
        final_head=_text(_git(root, "rev-parse", "HEAD")),
        final_remote=publication_git.live_branch_sha(
            fetch_url,
            branch,
            phase="ci-failure-final-live-remote",
        ),
        working_tree_clean=clean,
    )
    receipt.update(
        {
            "outcome": "FAIL",
            "failedStep": "exact-SHA CI",
            "failure": failure,
            "ci": ci,
            "finalIssueState": "not queried",
            "receiptPath": receipt_path.relative_to(root).as_posix(),
        }
    )
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(
        f"{json.dumps(receipt, indent=2, sort_keys=True)}\n",
        encoding="utf-8",
    )
    return receipt_path


def _wait_for_exact_validation_run(
    github: GitHubBoundary,
    *,
    branch: str,
    approved_sha: str,
    timeout_seconds: float,
    poll_seconds: float,
) -> Mapping[str, object]:
    """Select the newest exact-SHA Validate run and wait for its terminal state."""

    deadline = time.monotonic() + timeout_seconds
    selected_id: int | None = None
    while True:
        if selected_id is None:
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                raise PublicationError("exact-SHA CI could not be identified or timed out")
            exact_run_ids = [
                run_id
                for run in github.list_validation_runs(
                    branch,
                    timeout_seconds=remaining_seconds,
                )
                if run.get("headSha") == approved_sha
                and run.get("workflowName") == "Validate"
                and isinstance((run_id := run.get("databaseId")), int)
            ]
            if exact_run_ids:
                selected_id = max(exact_run_ids)
        if selected_id is not None:
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                raise PublicationError("exact-SHA CI could not be identified or timed out")
            run = github.get_validation_run(
                selected_id,
                timeout_seconds=remaining_seconds,
            )
            if run.get("databaseId") != selected_id:
                raise PublicationError("exact-SHA CI returned a different run ID")
            if run.get("headSha") != approved_sha or run.get("workflowName") != "Validate":
                raise PublicationError("exact-SHA CI identity changed while waiting")
            if run.get("status") == "completed":
                _validate_exact_sha_ci_evidence(run)
                return run
        if time.monotonic() >= deadline:
            raise PublicationError("exact-SHA CI could not be identified or timed out")
        time.sleep(poll_seconds)


def _validate_exact_sha_ci_evidence(run: Mapping[str, object]) -> None:
    """Require the repository's complete terminal Validate workflow evidence."""

    if run.get("status") != "completed":
        raise PublicationError("exact-SHA CI workflow is not terminal")
    conclusion = run.get("conclusion")
    if conclusion != "success":
        raise PublicationError(f"exact-SHA CI concluded {conclusion}")
    jobs = run.get("jobs")
    if not isinstance(jobs, list) or not jobs or not all(isinstance(job, dict) for job in jobs):
        raise PublicationError("exact-SHA CI jobs evidence is missing or malformed")
    if len(jobs) != 1 or jobs[0].get("name") != "validate":
        raise PublicationError("exact-SHA CI expected validate job inventory is missing")
    validate_job = jobs[0]
    if validate_job.get("status") != "completed":
        raise PublicationError("exact-SHA CI validate job is not terminal")
    job_conclusion = validate_job.get("conclusion")
    if job_conclusion != "success":
        raise PublicationError(f"exact-SHA CI validate job concluded {job_conclusion}")


def _parser() -> argparse.ArgumentParser:
    """Build the narrow repository harness command surface."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "review-state"):
        command = subcommands.add_parser(name)
        command.add_argument("--base", required=True)
    validation = subcommands.add_parser("validate")
    validation.add_argument("--base", required=True)
    validation.add_argument("--protected", action="store_true")
    publication = subcommands.add_parser("publish")
    publication.add_argument("--issue", required=True, type=int)
    publication.add_argument("--base", required=True)
    publication.add_argument("--sha", required=True)
    publication.add_argument("--branch", required=True)
    publication.add_argument("--ci-timeout-seconds", type=float, default=1200)
    publication.add_argument("--ci-poll-seconds", type=float, default=10)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    """Run one harness capability and keep successful stdout compact."""

    options = _parser().parse_args(arguments)
    repository = Path(options.repo)
    base = str(options.base)
    if options.command == "preflight":
        print(json.dumps(collect_preflight(repository, base), sort_keys=True))
        return 0
    if options.command == "review-state":
        review = create_review_state(repository, base)
        output = {
            "status": "READY",
            "baseSha": review.snapshot.base_sha,
            "headSha": review.snapshot.head_sha,
            "snapshotDigest": review.snapshot.digest,
            "changedFiles": _changed_file_data(review.changed_files),
            "artifactPath": str(review.artifact_path),
            "receiptPath": str(review.receipt_path),
            "validation": _review_validation_data(review.validation),
        }
        print(json.dumps(output, sort_keys=True))
        return 0
    if options.command == "publish":
        issue = int(options.issue)
        approved_sha = str(options.sha)
        log_directory = (
            repository / OUTPUT_DIRECTORY / "logs" / "publication" / f"issue-{issue}-{approved_sha}"
        )
        _, _, repository_identity = _publication_destinations(repository)
        publication_result = publish_repository(
            repository,
            issue=issue,
            base=base,
            approved_sha=approved_sha,
            branch=str(options.branch),
            github=GitHubCli(repository, log_directory, repository_identity),
            ci_timeout_seconds=float(options.ci_timeout_seconds),
            ci_poll_seconds=float(options.ci_poll_seconds),
        )
        ci = publication_result["ci"]
        assert isinstance(ci, dict)
        print(
            f"PASS issue={issue} sha={approved_sha} ci-run={ci['runId']} "
            f"receipt={publication_result['receiptPath']}"
        )
        return 0
    result = validate_repository(
        repository,
        base,
        protected=bool(options.protected),
    )
    print(format_validation_result(result))
    if result.outcome != "PASS":
        failed_results = [check for check in result.checks if check.status == "FAIL"]
        if failed_results:
            failed = failed_results[-1]
            print(f"detailed-log={failed.log_path}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from error
