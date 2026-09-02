"""Create deterministic, snapshot-bound repository agent evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from base64 import b64encode
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SCRIPT_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_REPOSITORY_ROOT))

SNAPSHOT_SCHEMA = "core-console-agent-snapshot/v1"
OUTPUT_DIRECTORY = ".agent"
REVIEW_SCHEMA = "core-console-agent-review/v2"
VALIDATION_SCHEMA = "core-console-agent-validation/v1"
PREFLIGHT_SCHEMA = "core-console-agent-preflight/v1"


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
