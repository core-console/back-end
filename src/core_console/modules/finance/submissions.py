"""Finance v1 create admission and atomic execution; explicit retries own recovery."""

from collections.abc import Awaitable, Callable
from datetime import UTC
from hashlib import sha256
from re import fullmatch
from typing import cast
from uuid import UUID

from pydantic import JsonValue, TypeAdapter, ValidationError
from sqlalchemy import BigInteger, func, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import Headers

from core_console.modules.finance.models import FinanceLedger
from core_console.modules.finance.service import (
    FinanceCategoryNameConflictError,
    FinanceLedgerNameConflictError,
    FinanceLedgerNotFoundError,
    InvalidFinanceAccountMoneyError,
    InvalidFinanceAccountNameError,
    InvalidFinanceAccountTrackingStartDateError,
    execute_create_finance_account,
    execute_create_finance_category,
    execute_create_finance_ledger,
)
from core_console.modules.finance.submission_commands import (
    AccountCommandV1,
    CategoryCommandV1,
    CreateOperation,
    LedgerCommandV1,
)
from core_console.modules.finance.submission_models import FinanceSubmission
from core_console.modules.finance.submission_schemas import (
    AccountCommandValidationRejection,
    AccountValidationProblem,
    CategoryCommandValidationRejection,
    CategoryValidationProblem,
    FinanceSubmissionResponse,
    FinanceSubmissionTerminal,
    LedgerCommandValidationRejection,
    LedgerValidationProblem,
    SubmissionReceipt,
    SubmissionUnfinished,
    SubmissionValidationProblem,
)
from core_console.modules.users.models import User
from core_console.problems import ApplicationProblem

# Measured through protected PostgreSQL contention tests. SET LOCAL scopes these
# budgets to each transaction, including uniqueness waits and Finance locks.
ADMISSION_LOCK_TIMEOUT_MS = 250
EXECUTION_LOCK_TIMEOUT_MS = 250
OPEN_ADMISSION_VERSIONS = frozenset({"1"})


def submission_problem(status: int, code: str, detail: str) -> ApplicationProblem:
    """Stable nonterminal protocol failures carry no receipt."""
    from http import HTTPStatus

    return ApplicationProblem(
        status=status, title=HTTPStatus(status).phrase, code=code, detail=detail
    )


def _header(headers: Headers, name: str) -> str:
    values = headers.getlist(name)
    if not values:
        raise submission_problem(
            400,
            "finance_submission_protocol_required",
            "Update the client: Finance creates require submission protocol headers.",
        )
    if len(values) != 1 or not values[0] or values[0] != values[0].strip() or "," in values[0]:
        raise submission_problem(
            400, "finance_submission_protocol_invalid", "Invalid protocol header."
        )
    return values[0]


def assert_submission_owner(headers: Headers, owner_id: UUID) -> None:
    try:
        asserted_owner = UUID(_header(headers, "Finance-Submission-Owner"))
    except ValueError:
        raise submission_problem(
            400, "finance_submission_protocol_invalid", "Invalid owner UUID."
        ) from None
    if asserted_owner != owner_id:
        raise submission_problem(
            403,
            "finance_submission_owner_mismatch",
            "Submission owner does not match Current User.",
        )


def create_protocol_headers(headers: Headers, owner_id: UUID) -> tuple[UUID, str]:
    """Reject ambiguity and assert the prepared owner before binding access."""
    raw_key = _header(headers, "Idempotency-Key")
    version = _header(headers, "Finance-Command-Version")
    try:
        key = UUID(raw_key)
    except ValueError:
        raise submission_problem(
            400, "finance_submission_protocol_invalid", "Invalid submission UUID."
        ) from None
    if key.version != 4 or fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", version) is None:
        raise submission_problem(
            400, "finance_submission_protocol_invalid", "Invalid submission key or command version."
        )
    assert_submission_owner(headers, owner_id)
    return key, version


async def _binding(
    session: AsyncSession, owner_id: UUID, key: UUID, *, lock: bool = False
) -> FinanceSubmission | None:
    query = (
        select(FinanceSubmission)
        .where(FinanceSubmission.local_user_id == owner_id, FinanceSubmission.submission_id == key)
        .execution_options(populate_existing=True)
    )
    if lock:
        query = query.with_for_update()
    return await session.scalar(query)


async def _authorize_binding(session: AsyncSession, row: FinanceSubmission) -> None:
    if row.retention_ledger_id is not None:
        accessible = await session.scalar(
            select(FinanceLedger.id).where(
                FinanceLedger.id == row.retention_ledger_id,
                FinanceLedger.owner_id == row.local_user_id,
            )
        )
        if accessible is None:
            raise submission_problem(
                404, "finance_submission_not_found", "Submission is unavailable."
            )


def _compare(
    row: FinanceSubmission, version: str, command: dict[str, object] | None = None
) -> None:
    if row.command_version != version:
        raise submission_problem(
            409,
            "finance_submission_version_mismatch",
            "Submission command version differs from its binding.",
        )
    if command is not None and row.canonical_command != command:
        raise submission_problem(
            409,
            "finance_submission_content_conflict",
            "Submission is bound to a different command.",
        )


def _receipt(row: FinanceSubmission) -> SubmissionReceipt:
    assert row.resolved_at is not None and row.terminal_outcome is not None
    return TypeAdapter(SubmissionReceipt).validate_python(
        {
            "submissionId": row.submission_id,
            "commandVersion": row.command_version,
            "operation": row.canonical_command["operation"],
            "targetLedgerId": row.canonical_command["targetLedgerId"],
            "admittedAt": row.admitted_at.astimezone(UTC),
            "resolvedAt": row.resolved_at.astimezone(UTC),
            "outcome": row.terminal_outcome,
        }
    )


async def _lock_budget(session: AsyncSession, milliseconds: int) -> None:
    await session.execute(select(func.set_config("lock_timeout", f"{milliseconds}ms", True)))


async def _lock_submission_key(session: AsyncSession, owner_id: UUID, key: UUID) -> None:
    """Serialize admission and Q29 proof for one owner/key pair."""
    await _lock_budget(session, ADMISSION_LOCK_TIMEOUT_MS)
    digest = sha256(b"finance-submission-v1\0" + owner_id.bytes + key.bytes).digest()
    lock_id = int.from_bytes(digest[:8], byteorder="big", signed=True)
    await session.execute(select(func.pg_advisory_xact_lock(sql_cast(lock_id, BigInteger))))


async def _authorize_target(session: AsyncSession, owner_id: UUID, target: UUID | None) -> None:
    if target is not None:
        ledger = await session.scalar(
            select(FinanceLedger.id).where(
                FinanceLedger.id == target,
                FinanceLedger.owner_id == owner_id,
            )
        )
        if ledger is None:
            raise FinanceLedgerNotFoundError


async def submit_create(
    session: AsyncSession,
    *,
    owner_id: UUID,
    key: UUID,
    version: str,
    operation: CreateOperation,
    target_ledger_id: UUID | None,
    read_body: Callable[[], Awaitable[object]],
) -> SubmissionReceipt | SubmissionValidationProblem:
    """Commit binding before executing; never use the committing create wrapper."""
    try:
        return await _submit_create(
            session,
            owner_id=owner_id,
            key=key,
            version=version,
            operation=operation,
            target_ledger_id=target_ledger_id,
            read_body=read_body,
        )
    except Exception as exc:
        await session.rollback()
        if isinstance(exc, OperationalError) and getattr(exc.orig, "sqlstate", None) == "55P03":
            raise submission_problem(
                409,
                "finance_submission_busy",
                "Submission serialization wait exceeded its bounded budget.",
            ) from None
        raise


async def _submit_create(
    session: AsyncSession,
    *,
    owner_id: UUID,
    key: UUID,
    version: str,
    operation: CreateOperation,
    target_ledger_id: UUID | None,
    read_body: Callable[[], Awaitable[object]],
) -> SubmissionReceipt | SubmissionValidationProblem:
    await _authorize_target(session, owner_id, target_ledger_id)
    row = await _binding(session, owner_id, key)
    if row is not None:
        await _authorize_binding(session, row)
        _compare(row, version)
    if version != "1":
        raise submission_problem(
            422, "finance_command_version_unsupported", "Command version is not supported."
        )
    if row is None and version not in OPEN_ADMISSION_VERSIONS:
        raise submission_problem(
            409, "finance_command_version_closed", "Command version is closed to new admissions."
        )
    attempted_body = await read_body()
    try:
        parsers: dict[
            CreateOperation,
            type[LedgerCommandV1] | type[CategoryCommandV1] | type[AccountCommandV1],
        ] = {
            "createFinanceLedger": LedgerCommandV1,
            "createFinanceAccount": AccountCommandV1,
            "createFinanceCategory": CategoryCommandV1,
        }
        parser = parsers[operation]
        body = parser.model_validate(attempted_body)
    except ValidationError as exc:
        # A known binding blocks Q29, even for an invalid different attempt.
        if row is not None:
            raise submission_problem(
                409,
                "finance_submission_content_conflict",
                "Submission is bound to a different command.",
            ) from None
        problem_data: dict[str, object] = {
            "type": "about:blank",
            "title": "Unprocessable Entity",
            "status": 422,
            "code": "validation_error",
            "detail": "The request did not satisfy the API contract.",
            "errors": [
                {**error, "loc": ["body", *error["loc"]]}
                for error in exc.errors(include_context=False, include_url=False)
            ],
        }
        if not isinstance(attempted_body, dict):
            # No exact parsed-object correlation: ordinary unresolved validation.
            raise submission_problem(
                422, "validation_error", "The request must be a JSON object."
            ) from None
        await _lock_submission_key(session, owner_id, key)
        await _authorize_target(session, owner_id, target_ledger_id)
        row = await _binding(session, owner_id, key)
        if row is not None:
            await _authorize_binding(session, row)
            _compare(row, version)
            raise submission_problem(
                409,
                "finance_submission_content_conflict",
                "Submission is bound to a different command.",
            ) from None
        proof_models: dict[
            CreateOperation,
            (
                type[LedgerCommandValidationRejection]
                | type[AccountCommandValidationRejection]
                | type[CategoryCommandValidationRejection]
            ),
        ] = {
            "createFinanceLedger": LedgerCommandValidationRejection,
            "createFinanceAccount": AccountCommandValidationRejection,
            "createFinanceCategory": CategoryCommandValidationRejection,
        }
        proof_model = proof_models[operation]
        problem_data["commandValidationRejection"] = proof_model.model_validate(
            {
                "kind": "definitivelyNotAdmitted",
                "submissionId": key,
                "commandVersion": "1",
                "ownerId": owner_id,
                "operation": operation,
                "targetLedgerId": target_ledger_id,
                "attemptedBody": cast(dict[str, JsonValue], attempted_body),
            }
        )
        await session.rollback()
        problem_models: dict[
            CreateOperation,
            (
                type[LedgerValidationProblem]
                | type[AccountValidationProblem]
                | type[CategoryValidationProblem]
            ),
        ] = {
            "createFinanceLedger": LedgerValidationProblem,
            "createFinanceAccount": AccountValidationProblem,
            "createFinanceCategory": CategoryValidationProblem,
        }
        problem_model = problem_models[operation]
        return problem_model.model_validate(problem_data)
    command: dict[str, object] = {
        "commandVersion": version,
        "operation": operation,
        "targetLedgerId": str(target_ledger_id) if target_ledger_id is not None else None,
        "body": body.canonical() if isinstance(body, AccountCommandV1) else {"name": body.name},
    }
    if row is not None:
        _compare(row, version, command)
        if row.terminal_outcome is not None:
            return _receipt(row)
    else:
        await _lock_submission_key(session, owner_id, key)
        await _authorize_target(session, owner_id, target_ledger_id)
        row = await _binding(session, owner_id, key)
        if row is None:
            await session.execute(
                insert(FinanceSubmission)
                .values(
                    local_user_id=owner_id,
                    submission_id=key,
                    command_version=version,
                    canonical_command=command,
                    retention_ledger_id=target_ledger_id,
                )
                .on_conflict_do_nothing(index_elements=["local_user_id", "submission_id"])
            )
            row = await _binding(session, owner_id, key)
        assert row is not None
        await _authorize_binding(session, row)
        _compare(row, version, command)
    # Also closes the Current User resolution transaction before execution.
    await session.commit()
    await _lock_budget(session, EXECUTION_LOCK_TIMEOUT_MS)
    row = await _binding(session, owner_id, key, lock=True)
    assert row is not None
    await _authorize_binding(session, row)
    _compare(row, version, command)
    if row.terminal_outcome is not None:
        receipt = _receipt(row)
        await session.rollback()
        return receipt
    await _authorize_target(session, owner_id, target_ledger_id)
    active = await session.scalar(select(User.status).where(User.id == owner_id))
    if active != "active":
        raise submission_problem(403, "access_denied", "Access is denied.")
    try:
        async with session.begin_nested():
            if isinstance(body, AccountCommandV1):
                assert target_ledger_id is not None
                balance = await execute_create_finance_account(
                    session,
                    owner_id=owner_id,
                    ledger_id=target_ledger_id,
                    name=body.name,
                    nature=body.nature,
                    currency=body.currency,
                    opening_balance=body.opening_balance.to_money(),
                    tracking_start_date=body.tracking_start_date,
                )
                resource_id = balance.account.id
                resource_type = "account"
            elif operation == "createFinanceCategory":
                assert target_ledger_id is not None
                category = await execute_create_finance_category(
                    session, owner_id=owner_id, ledger_id=target_ledger_id, name=body.name
                )
                resource_id = category.id
                resource_type = "category"
            else:
                ledger = await execute_create_finance_ledger(
                    session, owner_id=owner_id, name=body.name
                )
                resource_id = ledger.id
                resource_type = "ledger"
    except (FinanceLedgerNameConflictError, FinanceCategoryNameConflictError) as exc:
        # Only recognized business failures become terminal, after savepoint rollback.
        resource_type = "ledger" if isinstance(exc, FinanceLedgerNameConflictError) else "category"
        row.terminal_outcome = {
            "kind": "rejected",
            "problem": {
                "type": "about:blank",
                "title": "Conflict",
                "status": 409,
                "code": f"finance_{resource_type}_name_conflict",
                "detail": f"A Finance {resource_type.title()} with this name already exists.",
            },
        }
    except (
        InvalidFinanceAccountMoneyError,
        InvalidFinanceAccountNameError,
        InvalidFinanceAccountTrackingStartDateError,
    ) as exc:
        # Access and unknown failures remain unfinished. These recognized
        # Account business failures keep their existing stable 422 semantics.
        if operation != "createFinanceAccount":
            raise
        row.terminal_outcome = {
            "kind": "rejected",
            "problem": {
                "type": "about:blank",
                "title": "Unprocessable Entity",
                "status": 422,
                "code": "validation_error",
                "detail": str(exc),
            },
        }
    else:
        row.retention_ledger_id = target_ledger_id or resource_id
        row.terminal_outcome = {
            "kind": "created",
            "resource": {"type": resource_type, "id": str(resource_id)},
        }
    with session.no_autoflush:
        row.resolved_at = await session.scalar(select(func.clock_timestamp()))
    await session.flush()
    receipt = _receipt(row)  # Projection errors precede commit and remain nonterminal.
    await session.commit()
    return receipt


async def lookup_submission(
    session: AsyncSession, *, owner_id: UUID, key: UUID
) -> FinanceSubmissionResponse:
    """Read evidence only, with the same safe result for absent/inaccessible IDs."""
    row = await _binding(session, owner_id, key)
    if row is None:
        raise submission_problem(404, "finance_submission_not_found", "Submission is unavailable.")
    await _authorize_binding(session, row)
    if row.terminal_outcome is not None:
        return FinanceSubmissionResponse(
            FinanceSubmissionTerminal(state="terminal", receipt=_receipt(row))
        )
    return FinanceSubmissionResponse(
        TypeAdapter(SubmissionUnfinished).validate_python(
            {
                "state": "unfinished",
                "submissionId": row.submission_id,
                "commandVersion": row.command_version,
                "operation": row.canonical_command["operation"],
                "targetLedgerId": row.canonical_command["targetLedgerId"],
                "admittedAt": row.admitted_at.astimezone(UTC),
            }
        )
    )
