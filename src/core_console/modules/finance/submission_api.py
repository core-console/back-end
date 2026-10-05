"""Finance create submission HTTP boundary, including guarded pre-admission validation."""

import json
from math import isfinite
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core_console.database.dependencies import get_session
from core_console.modules.finance.api import _run_finance_workflow
from core_console.modules.finance.submission_commands import (
    AccountCommandV1,
    CategoryCommandV1,
    CreateOperation,
    LedgerCommandV1,
    TransactionCommandV1,
)
from core_console.modules.finance.submission_schemas import (
    AccountCreatedReceipt,
    AccountRejectedOutcome,
    AccountTerminalProblem,
    AccountValidationProblem,
    AccountValidationResponse,
    CategoryConflictResponse,
    CategoryCreatedReceipt,
    CategoryRejectedOutcome,
    CategoryTerminalProblem,
    CategoryValidationProblem,
    CategoryValidationResponse,
    FinanceSubmissionResponse,
    LedgerConflictResponse,
    LedgerCreatedReceipt,
    LedgerRejectedOutcome,
    LedgerTerminalProblem,
    LedgerValidationProblem,
    LedgerValidationResponse,
    SubmissionNonterminalProblem,
    TransactionArchivedTerminalProblem,
    TransactionConflictResponse,
    TransactionCreatedReceipt,
    TransactionInvalidTerminalProblem,
    TransactionMissingTerminalProblem,
    TransactionNotFoundResponse,
    TransactionRejectedOutcome,
    TransactionValidationProblem,
    TransactionValidationResponse,
)
from core_console.modules.finance.submissions import (
    assert_submission_owner,
    create_protocol_headers,
    lookup_submission,
    submission_problem,
    submit_create,
)
from core_console.modules.users.dependencies import require_active_user
from core_console.modules.users.identity import CurrentUser
from core_console.problems import ProblemDetails, problem_response

router = APIRouter(prefix="/api/finance", tags=["Finance"])


def _reject_non_json_number(value: str) -> None:
    raise ValueError(f"Non-JSON number: {value}")


def _parse_finite_json_float(value: str) -> float:
    parsed = float(value)
    if not isfinite(parsed):
        raise ValueError("JSON number is outside the supported finite range")
    return parsed


def _header_parameter(name: str, *, format: str | None = None) -> dict[str, object]:
    schema = {"type": "string"}
    if format is not None:
        schema["format"] = format
    return {"name": name, "in": "header", "required": True, "schema": schema}


def _account_request_schema() -> dict[str, object]:
    # Delayed parsing cannot use a FastAPI body dependency. Inline the only
    # nested schema so references resolve in the complete OpenAPI document.
    schema = AccountCommandV1.model_json_schema()
    definitions = schema.pop("$defs")
    schema["properties"]["openingBalance"] = definitions["OpeningBalanceV1"]
    return schema


@router.post(
    "/ledgers",
    operation_id="createFinanceLedger",
    summary="Create a Finance Ledger",
    description="Durably admits a Ledger v1 command, then returns its immutable terminal receipt.",
    status_code=201,
    response_model=LedgerCreatedReceipt,
    responses={
        400: {
            "model": ProblemDetails,
            "description": "Submission headers are required and must be valid.",
        },
        403: {"model": ProblemDetails, "description": "Access or owner assertion denied."},
        404: {"model": ProblemDetails, "description": "Stored submission scope is unavailable."},
        409: {
            "model": LedgerConflictResponse,
            "description": "Terminal name rejection or nonterminal submission conflict.",
        },
        422: {
            "model": LedgerValidationResponse,
            "description": "Unsupported version, generic validation, or guarded Q29 evidence.",
        },
        500: {
            "model": ProblemDetails,
            "description": "Unexpected failure; outcome remains unresolved.",
        },
        503: {"model": ProblemDetails, "description": "PostgreSQL is unavailable."},
    },
    openapi_extra={
        "security": [],
        "parameters": [
            _header_parameter("Idempotency-Key", format="uuid"),
            _header_parameter("Finance-Command-Version"),
            _header_parameter("Finance-Submission-Owner", format="uuid"),
        ],
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": LedgerCommandV1.model_json_schema()}},
        },
    },
)
async def post_ledger(
    request: Request,
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    return await _post_create(request, actor, session, "createFinanceLedger", None)


@router.post(
    "/ledgers/{ledgerId}/accounts",
    operation_id="createFinanceAccount",
    summary="Create a Finance Account",
    description="Admits an Account v1 command and returns its immutable terminal receipt.",
    status_code=201,
    response_model=AccountCreatedReceipt,
    responses={
        400: {
            "model": ProblemDetails,
            "description": "Submission headers are required and must be valid.",
        },
        403: {"model": ProblemDetails, "description": "Access or owner assertion denied."},
        404: {"model": ProblemDetails, "description": "Stored submission scope is unavailable."},
        409: {
            "model": SubmissionNonterminalProblem,
            "description": "Terminal business rejection or nonterminal submission conflict.",
        },
        422: {
            "model": AccountValidationResponse,
            "description": "Unsupported version, generic validation, or guarded Q29 evidence.",
        },
        500: {
            "model": ProblemDetails,
            "description": "Unexpected failure; outcome remains unresolved.",
        },
        503: {"model": ProblemDetails, "description": "PostgreSQL is unavailable."},
    },
    openapi_extra={
        "security": [],
        "parameters": [
            _header_parameter("Idempotency-Key", format="uuid"),
            _header_parameter("Finance-Command-Version"),
            _header_parameter("Finance-Submission-Owner", format="uuid"),
        ],
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": _account_request_schema()}},
        },
    },
)
async def post_account(
    request: Request,
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    return await _post_create(request, actor, session, "createFinanceAccount", ledger_id)


@router.post(
    "/ledgers/{ledgerId}/categories",
    operation_id="createFinanceCategory",
    summary="Create a Finance Category",
    description="Admits a Category v1 command and returns its immutable terminal receipt.",
    status_code=201,
    response_model=CategoryCreatedReceipt,
    responses={
        400: {
            "model": ProblemDetails,
            "description": "Submission headers are required and must be valid.",
        },
        403: {"model": ProblemDetails, "description": "Access or owner assertion denied."},
        404: {"model": ProblemDetails, "description": "Stored submission scope is unavailable."},
        409: {
            "model": CategoryConflictResponse,
            "description": "Terminal business rejection or nonterminal submission conflict.",
        },
        422: {
            "model": CategoryValidationResponse,
            "description": "Unsupported version, generic validation, or guarded Q29 evidence.",
        },
        500: {
            "model": ProblemDetails,
            "description": "Unexpected failure; outcome remains unresolved.",
        },
        503: {"model": ProblemDetails, "description": "PostgreSQL is unavailable."},
    },
    openapi_extra={
        "security": [],
        "parameters": [
            _header_parameter("Idempotency-Key", format="uuid"),
            _header_parameter("Finance-Command-Version"),
            _header_parameter("Finance-Submission-Owner", format="uuid"),
        ],
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": CategoryCommandV1.model_json_schema()}},
        },
    },
)
async def post_category(
    request: Request,
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    return await _post_create(request, actor, session, "createFinanceCategory", ledger_id)


def _transaction_request_schema() -> dict[str, object]:
    schema = TypeAdapter(TransactionCommandV1).json_schema()
    definitions = schema.pop("$defs")

    def inline(value: object) -> object:
        if isinstance(value, dict):
            if "$ref" in value:
                return inline(definitions[value["$ref"].rsplit("/", 1)[1]])
            # Inlined oneOf branches do not have component-reference mappings.
            return {key: inline(item) for key, item in value.items() if key != "mapping"}
        if isinstance(value, list):
            return [inline(item) for item in value]
        return value

    return cast(dict[str, object], inline(schema))


@router.post(
    "/ledgers/{ledgerId}/transactions",
    operation_id="createFinanceTransaction",
    summary="Create a Finance Transaction",
    description="Admits Income, Expense, or Internal Transfer v1 and returns an immutable receipt.",
    status_code=201,
    response_model=TransactionCreatedReceipt,
    responses={
        400: {"model": ProblemDetails, "description": "Submission headers are required and valid."},
        403: {"model": ProblemDetails, "description": "Access or owner assertion denied."},
        404: {
            "model": TransactionNotFoundResponse,
            "description": "Unavailable scope or terminal reference rejection.",
        },
        409: {
            "model": TransactionConflictResponse,
            "description": "Terminal archived rejection or unresolved conflict/busy.",
        },
        422: {
            "model": TransactionValidationResponse,
            "description": "Terminal rejection, guarded Q29, or unresolved validation/version.",
        },
        500: {
            "model": ProblemDetails,
            "description": "Unexpected failure; outcome remains unresolved.",
        },
        503: {"model": ProblemDetails, "description": "PostgreSQL is unavailable."},
    },
    openapi_extra={
        "security": [],
        "parameters": [
            _header_parameter("Idempotency-Key", format="uuid"),
            _header_parameter("Finance-Command-Version"),
            _header_parameter("Finance-Submission-Owner", format="uuid"),
        ],
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": _transaction_request_schema()}},
        },
    },
)
async def post_transaction(
    request: Request,
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    return await _post_create(request, actor, session, "createFinanceTransaction", ledger_id)


async def _post_create(
    request: Request,
    actor: CurrentUser,
    session: AsyncSession,
    operation: CreateOperation,
    target_ledger_id: UUID | None,
) -> JSONResponse:
    """Parse the body after access/headers; framework errors never assert Q29."""
    key, version = create_protocol_headers(request.headers, actor.id)

    async def read_body() -> object:
        try:
            return json.loads(
                await request.body(),
                parse_constant=_reject_non_json_number,
                parse_float=_parse_finite_json_float,
            )
        except ValueError, UnicodeError:
            raise submission_problem(
                422, "validation_error", "The request must contain valid JSON."
            ) from None

    evidence = await _run_finance_workflow(
        submit_create(
            session,
            owner_id=actor.id,
            key=key,
            version=version,
            operation=operation,
            target_ledger_id=target_ledger_id,
            read_body=read_body,
        )
    )
    if isinstance(
        evidence,
        (
            LedgerValidationProblem,
            AccountValidationProblem,
            CategoryValidationProblem,
            TransactionValidationProblem,
        ),
    ):
        return problem_response(evidence.model_copy(update={"instance": request.url.path}))
    if isinstance(
        evidence.outcome,
        (
            LedgerRejectedOutcome,
            AccountRejectedOutcome,
            CategoryRejectedOutcome,
            TransactionRejectedOutcome,
        ),
    ):
        problem = evidence.outcome.problem
        transaction_problem_models: dict[
            int,
            type[TransactionMissingTerminalProblem]
            | type[TransactionArchivedTerminalProblem]
            | type[TransactionInvalidTerminalProblem],
        ] = {
            404: TransactionMissingTerminalProblem,
            409: TransactionArchivedTerminalProblem,
            422: TransactionInvalidTerminalProblem,
        }
        problem_models: dict[
            CreateOperation,
            (
                type[LedgerTerminalProblem]
                | type[AccountTerminalProblem]
                | type[CategoryTerminalProblem]
                | type[TransactionArchivedTerminalProblem]
                | type[TransactionMissingTerminalProblem]
                | type[TransactionInvalidTerminalProblem]
            ),
        ] = {
            "createFinanceLedger": LedgerTerminalProblem,
            "createFinanceAccount": AccountTerminalProblem,
            "createFinanceCategory": CategoryTerminalProblem,
            "createFinanceTransaction": transaction_problem_models[problem.status],
        }
        problem_model = problem_models[operation]
        return problem_response(
            problem_model.model_validate(
                {
                    **problem.model_dump(),
                    "instance": request.url.path,
                    "submissionReceipt": evidence.model_dump(mode="json", by_alias=True),
                }
            )
        )
    return JSONResponse(status_code=201, content=evidence.model_dump(mode="json", by_alias=True))


@router.get(
    "/submissions/{submissionId}",
    operation_id="getFinanceSubmission",
    summary="Look up a known Finance submission",
    response_model=FinanceSubmissionResponse,
    responses={
        status: {"model": ProblemDetails, "description": description}
        for status, description in {
            400: "Owner header is missing or invalid.",
            403: "Access or owner assertion denied.",
            404: "Submission is absent or inaccessible.",
            422: "Submission UUID is invalid.",
            500: "Unexpected failure.",
            503: "PostgreSQL is unavailable.",
        }.items()
    },
    openapi_extra={
        "security": [],
        "parameters": [_header_parameter("Finance-Submission-Owner", format="uuid")],
    },
)
async def get_submission(
    request: Request,
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    submission_id: Annotated[UUID, Path(alias="submissionId")],
) -> FinanceSubmissionResponse:
    assert_submission_owner(request.headers, actor.id)
    return await _run_finance_workflow(
        lookup_submission(session, owner_id=actor.id, key=submission_id)
    )


class SubmissionLookupNoStoreMiddleware:
    """Apply no-store even to dependency, path, and infrastructure errors."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/finance/submissions/"):
            await self.app(scope, receive, send)
            return

        async def no_store(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (key, value)
                    for key, value in message["headers"]
                    if key.lower() != b"cache-control"
                ]
                message["headers"] = [*headers, (b"cache-control", b"no-store")]
            await send(message)

        await self.app(scope, receive, no_store)
