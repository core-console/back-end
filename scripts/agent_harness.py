"""Create deterministic, snapshot-bound repository agent evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
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

from scripts.database_diagnostics import DatabaseRedactor  # noqa: E402
from scripts.validation_evidence import (  # noqa: E402
    HISTORICAL_VALIDATION_SCHEMA,
    PYTEST_ARGS,
    VALIDATION_SCHEMA,
    execution_directory,
    inspect_pytest,
    postgres_target,
    read_object,
    reserve_execution,
    seal_receipt,
    sha256,
    timestamp,
)

SNAPSHOT_SCHEMA = "core-console-agent-snapshot/v1"
OUTPUT_DIRECTORY = ".agent"
REVIEW_SCHEMA = "core-console-agent-review/v3"
PREFLIGHT_SCHEMA = "core-console-agent-preflight/v1"
PUBLICATION_SCHEMA = "core-console-agent-publication/v1"
PUBLICATION_REMOTE_READ_ATTEMPTS = 3
PUBLICATION_REMOTE_READ_ATTEMPT_TIMEOUT_SECONDS = 5.0
PUBLICATION_REMOTE_READ_DEADLINE_SECONDS = 12.0

_NON_RETRYABLE_REMOTE_READ_MARKERS = (
    "access denied",
    "authentication failed",
    "certificate",
    "could not read username",
    "permission denied",
    "repository not found",
    "unable to get local issuer",
)
_TRANSIENT_REMOTE_READ_MARKERS = (
    "connection reset by peer",
    "connection timed out",
    "could not resolve host",
    "failed to connect",
    "failed to receive handshake",
    "http/2 stream",
    "operation timed out",
    "remote end hung up unexpectedly",
    "unexpected eof while reading",
)


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
    execution_id: str | None = None
    receipt_sha256: str | None = None
    evidence_class: str | None = None


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
        self._redact = DatabaseRedactor(root)

    def _run_once(
        self,
        phase: str,
        *arguments: str,
        timeout_seconds: float | None = None,
    ) -> tuple[subprocess.CompletedProcess[bytes], Path, bool]:
        self._command_index += 1
        command = ("git", *arguments)
        timed_out = False
        try:
            result = subprocess.run(
                command,
                cwd=self._root,
                check=False,
                capture_output=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            timed_out = True
            result = subprocess.CompletedProcess(
                command,
                -1,
                _command_output_bytes(error.stdout),
                _command_output_bytes(error.stderr),
            )
        log_path = self._log_directory / f"{self._command_index:02d}-{_safe_label(phase)}-git.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        timeout_note = f"\n[timeout]\n{timeout_seconds} seconds\n" if timed_out else ""
        transcript = _diagnostic_transcript(command, result.stdout, result.stderr, self._redact)
        log_path.write_text(transcript + timeout_note, encoding="utf-8")
        return result, log_path, timed_out

    def _run(self, phase: str, *arguments: str) -> bytes:
        result, log_path, timed_out = self._run_once(phase, *arguments)
        if timed_out:
            raise PublicationError(
                f"publication Git phase={phase} timed out; "
                f"detailed-log={log_path.relative_to(self._root).as_posix()}"
            )
        if result.returncode != 0:
            raise PublicationError(
                f"publication Git phase={phase} failed; "
                f"detailed-log={log_path.relative_to(self._root).as_posix()}"
            )
        return result.stdout

    def live_branch_sha(self, remote_url: str, branch: str, *, phase: str) -> str:
        deadline = time.monotonic() + PUBLICATION_REMOTE_READ_DEADLINE_SECONDS
        output: bytes | None = None
        last_log_path: Path | None = None
        for attempt in range(1, PUBLICATION_REMOTE_READ_ATTEMPTS + 1):
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                detail = (
                    f"; detailed-log={last_log_path.relative_to(self._root).as_posix()}"
                    if last_log_path is not None
                    else ""
                )
                raise PublicationError(
                    f"publication Git phase={phase} exceeded remote-read deadline{detail}"
                )
            result, log_path, timed_out = self._run_once(
                phase,
                "ls-remote",
                "--exit-code",
                remote_url,
                f"refs/heads/{branch}",
                timeout_seconds=min(
                    PUBLICATION_REMOTE_READ_ATTEMPT_TIMEOUT_SECONDS,
                    remaining_seconds,
                ),
            )
            last_log_path = log_path
            if result.returncode == 0 and not timed_out:
                output = result.stdout
                break
            transient = _is_transient_remote_read_failure(result.stderr, timed_out=timed_out)
            if not transient:
                raise PublicationError(
                    f"publication Git phase={phase} failed; "
                    f"detailed-log={log_path.relative_to(self._root).as_posix()}"
                )
            if attempt == PUBLICATION_REMOTE_READ_ATTEMPTS:
                raise PublicationError(
                    f"publication Git phase={phase} exhausted transient remote-read "
                    f"failures after {attempt} attempts; "
                    f"detailed-log={log_path.relative_to(self._root).as_posix()}"
                )
        if output is None:
            raise PublicationError(f"publication Git phase={phase} remote read failed")
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


def _is_transient_remote_read_failure(stderr: bytes, *, timed_out: bool = False) -> bool:
    """Classify only known transport failures as safe read-only retry candidates."""

    diagnostic = stderr.decode("utf-8", errors="replace").lower()
    if any(marker in diagnostic for marker in _NON_RETRYABLE_REMOTE_READ_MARKERS):
        return False
    return timed_out or any(marker in diagnostic for marker in _TRANSIENT_REMOTE_READ_MARKERS)


class GitHubCli:
    """GitHub CLI adapter that retains verbose responses outside stdout."""

    def __init__(self, root: Path, log_directory: Path, repository: str) -> None:
        self._root = root
        self._log_directory = log_directory
        self._repository = repository
        self._command_index = 0
        self._redact = DatabaseRedactor(root)

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
            _diagnostic_transcript(command, stdout, stderr, self._redact), encoding="utf-8"
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
        diagnostic = f"git {' '.join(arguments)} failed: {message}"
        raise RuntimeError(DatabaseRedactor(repository)(diagnostic))
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
        "executionId": validation.execution_id,
        "receiptSha256": validation.receipt_sha256,
        "evidenceClass": validation.evidence_class,
    }


def _repository_identity(root: Path) -> str:
    origins = _git_config_values(root, "remote.origin.url")
    if not origins:
        return f"local:{root.as_posix()}"
    identity = _github_repository_identity_from_url(origins[0]) if len(origins) == 1 else None
    return identity or f"origin-sha256:{hashlib.sha256(json.dumps(origins).encode()).hexdigest()}"


def _validation_identity(root: Path, snapshot: RepositorySnapshot) -> dict[str, object]:
    return {
        "schema": VALIDATION_SCHEMA,
        "repository": _repository_identity(root),
        "baseSha": snapshot.base_sha,
        "headSha": snapshot.head_sha,
        "snapshotDigest": snapshot.digest,
    }


def _validation_path(root: Path, snapshot: RepositorySnapshot, execution_id: str) -> Path:
    return (
        root / OUTPUT_DIRECTORY / "receipts" / f"validation-{snapshot.digest}-{execution_id}.json"
    )


def _check_configuration(checks: Sequence[ValidationCheck]) -> list[dict[str, object]]:
    return [{"label": check.label, "command": list(check.command)} for check in checks]


def _read_execution(
    root: Path, snapshot: RepositorySnapshot, execution_id: str
) -> tuple[dict[str, object], str | None]:
    """Recheck the execution envelope and all files required for a claimed PASS."""
    directory = execution_directory(root / OUTPUT_DIRECTORY, execution_id)
    if directory is None:
        return {}, None
    start_path = directory / "started.json"
    start = read_object(start_path)
    identity = _validation_identity(root, snapshot) | {"executionId": execution_id}
    if any(start.get(key) != value for key, value in identity.items()):
        return {}, None
    receipt_path = _validation_path(root, snapshot, execution_id)
    data = read_object(receipt_path)
    if not data:
        return {}, "incomplete" if not receipt_path.exists() else None
    if any(data.get(key) != value for key, value in start.items()) or (
        data.get("invocationSha256") != sha256(start_path)
        or data.get("status") not in ("completed", "interrupted")
        or (data.get("status") != "completed" and data.get("outcome") == "PASS")
        or not isinstance(data.get("completedAt"), str)
        or data.get("outcome") not in ("PASS", "FAIL", "STALE")
    ):
        return {}, None
    if data["outcome"] == "PASS":
        try:
            checks = data["checks"]
            configuration = start["configuration"]
            if (
                data.get("failedCheck") is not None
                or data.get("snapshotAfterDigest") != snapshot.digest
                or data.get("snapshotUnchanged") is not True
                or not isinstance(checks, list)
                or not isinstance(configuration, list)
                or len(checks) != len(configuration)
            ):
                return {}, None
            for index, (check, expected) in enumerate(zip(checks, configuration, strict=True), 1):
                log = directory / f"{index:02d}-{_safe_label(expected['label'])}.log"
                if check != {
                    "label": expected["label"],
                    "status": "PASS",
                    "returnCode": 0,
                    "logPath": log.relative_to(root).as_posix(),
                    "logSha256": sha256(log),
                }:
                    return {}, None
            if start["profile"] == "protected":
                executable = start.get("pythonExecutable")
                if not isinstance(executable, str) or not executable:
                    return {}, None
                canonical = _validation_checks(
                    snapshot.base_sha,
                    snapshot.head_sha,
                    None,
                    protected=True,
                    python_executable=executable,
                )
                proof_data, proof = inspect_pytest(start_path)
                if (
                    configuration != _check_configuration(canonical)
                    or not proof
                    or data.get("pytest") != proof_data
                    or data.get("protectedPostgresql") != {"requested": True, "exercised": True}
                ):
                    return {}, None
            elif start["profile"] not in ("standard", "custom") or data.get(
                "protectedPostgresql"
            ) != {"requested": False, "exercised": False}:
                return {}, None
        except KeyError, OSError, TypeError, ValueError:
            return {}, None
    purpose = start.get("purpose")
    if purpose not in ("current", "recovery"):
        return {}, None
    return data, "interrupted" if data["status"] == "interrupted" else str(purpose)


def _validation_binding(
    root: Path,
    snapshot: RepositorySnapshot,
    execution_id: str | None = None,
) -> ReviewValidationState:
    """Prefer the latest current protected start, with explicit selection by execution ID."""
    output = root / OUTPUT_DIRECTORY
    receipts = output / "receipts"
    if execution_id is None:
        candidates: list[tuple[bool, str, str]] = []
        identity = _validation_identity(root, snapshot)
        for start_path in (output / "validation").glob("*/started.json"):
            start = read_object(start_path)
            started = start.get("startedAt")
            if (
                isinstance(started, str)
                and start.get("purpose") == "current"
                and start.get("executionId") == start_path.parent.name
                and all(start.get(key) == value for key, value in identity.items())
            ):
                candidates.append(
                    (start.get("profile") == "protected", started, start_path.parent.name)
                )
        if candidates:
            execution_id = max(candidates)[2]

    if execution_id is not None:
        data, evidence_class = _read_execution(root, snapshot, execution_id)
        if data:
            path = _validation_path(root, snapshot, execution_id)
            protected = data.get("protectedPostgresql")
            outcome = data["outcome"]
            if (
                isinstance(protected, dict)
                and isinstance(protected.get("requested"), bool)
                and isinstance(protected.get("exercised"), bool)
                and outcome in ("PASS", "FAIL", "STALE")
            ):
                return ReviewValidationState(
                    "matching"
                    if data.get("snapshotAfterDigest") == snapshot.digest
                    and data.get("snapshotUnchanged") is True
                    else "stale",
                    outcome,
                    protected["requested"],
                    protected["exercised"],
                    path.relative_to(root).as_posix(),
                    execution_id,
                    sha256(path),
                    evidence_class,
                )
        if evidence_class == "incomplete":
            return ReviewValidationState(
                "matching", None, None, None, None, execution_id, None, evidence_class
            )
        return ReviewValidationState("stale", None, None, None, None, execution_id)

    legacy_path = receipts / f"validation-{snapshot.digest}.json"
    legacy = read_object(legacy_path)
    if (
        legacy.get("schema") == HISTORICAL_VALIDATION_SCHEMA
        and legacy.get("baseSha") == snapshot.base_sha
        and legacy.get("headSha") == snapshot.head_sha
        and legacy.get("snapshotDigest") == legacy.get("snapshotAfterDigest") == snapshot.digest
        and legacy.get("snapshotUnchanged") is True
    ):
        outcome = legacy.get("outcome")
        protected = legacy.get("protectedPostgresql")
        if outcome in ("PASS", "FAIL", "STALE") and isinstance(protected, dict):
            return ReviewValidationState(
                "matching",
                outcome,
                protected.get("requested"),
                protected.get("exercised"),
                legacy_path.relative_to(root).as_posix(),
                None,
                sha256(legacy_path),
                "historical-v1",
            )
    available = any(receipts.glob("validation-*.json")) or any(
        (output / "validation").glob("*/started.json")
    )
    return ReviewValidationState("stale" if available else "absent", None, None, None, None)


def create_review_state(
    repository: Path, base: str, *, execution_id: str | None = None
) -> ReviewState:
    """Write complete worktree-aware review evidence for one snapshot."""
    root = Path(_text(_git(repository, "rev-parse", "--show-toplevel")))
    snapshot = snapshot_repository(root, base)
    changed_files = _changed_files(root, snapshot.base_sha, snapshot.head_sha)
    untracked_paths = [item.path for item in changed_files if item.untracked]
    output_root = root / OUTPUT_DIRECTORY
    artifact_path = output_root / "reviews" / f"v3-{snapshot.digest}.json"
    receipt_path = output_root / "receipts" / f"review-v3-{snapshot.digest}.json"

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
    validation = _validation_binding(root, snapshot, execution_id)
    receipt: dict[str, object] = {
        "schema": REVIEW_SCHEMA,
        "baseSha": snapshot.base_sha,
        "headSha": snapshot.head_sha,
        "snapshotDigest": snapshot.digest,
        "changedFiles": _changed_file_data(changed_files),
        "artifactPath": artifact_path.relative_to(root).as_posix(),
        "artifactSha256": sha256(artifact_path),
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
    *,
    protected: bool = False,
    python_executable: str = sys.executable,
) -> tuple[ValidationCheck, ...]:
    """Extend the existing validation authority with worktree whitespace gates."""

    if authority_checks is None:
        from scripts.validate import CHECKS

        checks = tuple(
            ValidationCheck(
                label,
                (python_executable, *command[1:])
                if command[0] == sys.executable
                else tuple(command),
            )
            for label, command in CHECKS
        )
    else:
        checks = tuple(authority_checks)
    if protected and authority_checks is None:
        checks = tuple(
            ValidationCheck(
                "pytest",
                (
                    python_executable,
                    "-I",
                    "-B",
                    "-c",
                    "import sys; from pathlib import Path; sys.path.insert(0, str(Path.cwd())); "
                    "from scripts.protected_pytest import main; "
                    "raise SystemExit(main(Path(sys.argv[1])))",
                ),
            )
            if check.label == "pytest"
            else check
            for check in checks
        )
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


def _diagnostic_transcript(
    command: Sequence[str], stdout: bytes, stderr: bytes, redact: DatabaseRedactor
) -> str:
    # Command-line quoting can change individual credential spellings.
    safe_command = subprocess.list2cmdline(tuple(map(redact, command)))
    return (
        f"$ {safe_command}\n\n[stdout]\n"
        f"{redact.subprocess_text(stdout)}\n[stderr]\n"
        f"{redact.subprocess_text(stderr)}"
    )


def _write_check_log(
    path: Path,
    check: ValidationCheck,
    stdout: bytes,
    stderr: bytes,
    redact: DatabaseRedactor,
) -> None:
    """Retain sanitized quality-gate output outside normal agent context."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        output.write(_diagnostic_transcript(check.command, stdout, stderr, redact))


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


def validate_repository(
    repository: Path,
    base: str,
    *,
    protected: bool,
    checks: Sequence[ValidationCheck] | None = None,
    environment: Mapping[str, str] | None = None,
    purpose: Literal["current", "recovery"] = "current",
) -> ValidationResult:
    """Run ordered gates and seal the result for this execution alone."""
    root = Path(_text(_git(repository, "rev-parse", "--show-toplevel")))
    before = snapshot_repository(root, base)
    execution_id, directory = reserve_execution(root / OUTPUT_DIRECTORY)
    receipt_path = _validation_path(root, before, execution_id)
    start_path = directory / "started.json"
    selected_checks = _validation_checks(
        before.base_sha, before.head_sha, checks, protected=protected
    )
    command_environment = dict(os.environ if environment is None else environment)
    redact = DatabaseRedactor(root, command_environment)
    target = postgres_target(command_environment.get("TEST_DATABASE_URL")) if protected else None
    start = _validation_identity(root, before) | {
        "executionId": execution_id,
        "startedAt": timestamp(),
        "profile": "custom" if checks is not None else "protected" if protected else "standard",
        "purpose": purpose,
        "configuration": _check_configuration(
            selected_checks
            if checks is None
            else tuple(
                ValidationCheck(redact(check.label), tuple(map(redact, check.command)))
                for check in selected_checks
            )
        ),
        "pythonVersion": platform.python_version(),
        "pythonExecutable": sys.executable,
        "pytestArguments": list(PYTEST_ARGS) if protected else None,
        "postgresTarget": (
            {
                key: redact(value) if isinstance(value, str) else value
                for key, value in target.items()
            }
            if target is not None
            else None
        ),
    }
    seal_receipt(start_path, directory / "start.pending", start)

    check_results: list[ValidationCheckResult] = []
    proof_data: dict[str, object] | None = None
    proof = False
    failed_check: str | None = None
    status = "completed"
    if protected and target is None:
        failed_check = "protected PostgreSQL preflight"
    elif protected and checks is not None:
        failed_check = "protected validation requires the canonical check configuration"
    else:
        for index, check in enumerate(selected_checks, start=1):
            protected_pytest = protected and check.label == "pytest"
            arguments = (*check.command, str(start_path)) if protected_pytest else check.command
            try:
                completed = subprocess.run(
                    arguments,
                    cwd=root,
                    check=False,
                    capture_output=True,
                    env=command_environment,
                )
            except KeyboardInterrupt:
                failed_check = check.label if checks is None else redact(check.label)
                status = "interrupted"
                break
            except OSError:
                completed = subprocess.CompletedProcess(
                    arguments, 1, b"", b"Could not start the validation command.\n"
                )
            log_label = check.label if checks is None else redact(check.label)
            log_path = directory / f"{index:02d}-{_safe_label(log_label)}.log"
            _write_check_log(
                log_path,
                ValidationCheck(check.label, arguments),
                completed.stdout,
                completed.stderr,
                redact,
            )
            if protected_pytest:
                proof_data, verified = inspect_pytest(start_path)
                proof = completed.returncode == 0 and verified
            passed = completed.returncode == 0 and (not protected_pytest or proof)
            check_results.append(
                ValidationCheckResult(
                    log_label,
                    "PASS" if passed else "FAIL",
                    completed.returncode,
                    log_path,
                )
            )
            if not passed:
                failed_check = check.label if checks is None else redact(check.label)
                if protected_pytest and completed.returncode == 0:
                    failed_check = "pytest execution evidence"
                break

    after = snapshot_repository(root, base)
    unchanged = before == after and start["repository"] == _repository_identity(root)
    outcome: ValidationOutcome = "PASS" if failed_check is None else "FAIL"
    if not unchanged:
        outcome = "STALE"
        failed_check = "implementation snapshot changed during validation"
    result = ValidationResult(
        outcome,
        before,
        after,
        tuple(check_results),
        receipt_path,
        failed_check,
        _test_summary(check_results),
        proof,
    )
    receipt = start | {
        "status": status,
        "completedAt": timestamp(),
        "invocationSha256": sha256(start_path),
        "outcome": outcome,
        "snapshotAfterDigest": after.digest,
        "snapshotUnchanged": unchanged,
        "failedCheck": failed_check,
        "testSummary": result.test_summary,
        "protectedPostgresql": {"requested": protected, "exercised": proof},
        "pytest": proof_data,
        "checks": [
            {
                "label": check.label,
                "status": check.status,
                "returnCode": check.return_code,
                "logPath": check.log_path.relative_to(root).as_posix(),
                "logSha256": sha256(check.log_path),
            }
            for check in result.checks
        ],
    }
    seal_receipt(receipt_path, directory / "receipt.pending", receipt)
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
    issue: int | None = None,
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
        root
        / OUTPUT_DIRECTORY
        / "logs"
        / "publication"
        / _publication_identifier(issue, approved_sha)
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
    review_path = root / OUTPUT_DIRECTORY / "receipts" / f"review-v3-{snapshot.digest}.json"
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
    execution_id = (
        review_validation.get("executionId") if isinstance(review_validation, dict) else None
    )
    if not isinstance(execution_id, str):
        raise PublicationError("validation receipt is stale")
    binding = _validation_binding(root, snapshot, execution_id)
    if binding.snapshot_status != "matching" or binding.receipt_path is None:
        raise PublicationError("validation receipt is stale")
    if binding.outcome != "PASS":
        raise PublicationError("validation outcome is not PASS")
    if not (binding.protected_requested and binding.protected_exercised):
        raise PublicationError("protected PostgreSQL evidence is required")
    if binding.evidence_class != "current" or review_validation != _review_validation_data(binding):
        raise PublicationError("review-state receipt is stale")
    validation_path = root / binding.receipt_path
    artifact_relative = review.get("artifactPath")
    canonical_artifact = f"{OUTPUT_DIRECTORY}/reviews/v3-{snapshot.digest}.json"
    if artifact_relative != canonical_artifact:
        raise PublicationError("review-state receipt is stale")
    artifact_path = root / canonical_artifact
    artifact = _read_json_object(artifact_path, "review-state artifact is stale")
    if sha256(artifact_path) != review.get("artifactSha256"):
        raise PublicationError("review-state artifact is stale")
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

    final_issue_state = None
    if issue is not None:
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
        final_issue_state = issue_data.get("state")

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
            "finalIssueState": final_issue_state,
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


def _publication_identifier(issue: int | None, approved_sha: str) -> str:
    """Keep issue-linked and issue-free publication evidence in distinct paths."""

    linkage = "no-issue" if issue is None else f"issue-{issue}"
    return f"{linkage}-{approved_sha}"


def _publication_receipt_path(root: Path, issue: int | None, approved_sha: str) -> Path:
    """Return the stable idempotent receipt path for one authorized publication."""

    identifier = _publication_identifier(issue, approved_sha)
    return root / OUTPUT_DIRECTORY / "receipts" / f"publication-{identifier}.json"


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
    issue: int | None,
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
            "sha256": sha256(validation_path),
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
    issue: int | None,
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
            "finalIssueState": "not queried" if issue is not None else None,
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
    validation.add_argument("--purpose", choices=("current", "recovery"), default="current")
    subcommands.choices["review-state"].add_argument("--validation-execution")
    publication = subcommands.add_parser("publish")
    publication.add_argument(
        "--issue", type=int, help="explicit issue to verify and close after CI"
    )
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
        review = create_review_state(repository, base, execution_id=options.validation_execution)
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
        issue = int(options.issue) if options.issue is not None else None
        approved_sha = str(options.sha)
        log_directory = (
            repository
            / OUTPUT_DIRECTORY
            / "logs"
            / "publication"
            / _publication_identifier(issue, approved_sha)
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
        issue_label = str(issue) if issue is not None else "none"
        print(
            f"PASS issue={issue_label} sha={approved_sha} ci-run={ci['runId']} "
            f"receipt={publication_result['receiptPath']}"
        )
        return 0
    result = validate_repository(
        repository,
        base,
        protected=bool(options.protected),
        purpose=options.purpose,
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
