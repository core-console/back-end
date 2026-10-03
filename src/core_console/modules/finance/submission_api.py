"""Ledger submission HTTP boundary, including guarded pre-admission validation."""

import json
from math import isfinite
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core_console.database.dependencies import get_session
from core_console.modules.finance.api import _run_finance_workflow
from core_console.modules.finance.submission_schemas import (
    FinanceSubmissionResponse,
    LedgerConflictResponse,
    LedgerCreatedReceipt,
    LedgerRejectedOutcome,
    LedgerTerminalProblem,
    LedgerValidationProblem,
    LedgerValidationResponse,
)
from core_console.modules.finance.submissions import (
    LedgerCommandV1,
    assert_submission_owner,
    create_protocol_headers,
    lookup_submission,
    submission_problem,
    submit_ledger,
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
        submit_ledger(
            session,
            owner_id=actor.id,
            key=key,
            version=version,
            read_body=read_body,
        )
    )
    if isinstance(evidence, LedgerValidationProblem):
        return problem_response(evidence.model_copy(update={"instance": request.url.path}))
    if isinstance(evidence.outcome, LedgerRejectedOutcome):
        problem = evidence.outcome.problem
        return problem_response(
            LedgerTerminalProblem.model_validate(
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
