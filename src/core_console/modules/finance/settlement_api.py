"""Backend producer HTTP boundary for versioned Settlement workflows."""

from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.database.dependencies import get_session
from core_console.modules.finance.advances import _problem, require_advance_ledger
from core_console.modules.finance.api import _run_finance_workflow
from core_console.modules.finance.settlement_schemas import (
    SettlementCommandV1,
    SettlementDraftV1,
    SettlementPreview,
    SettlementResponse,
)
from core_console.modules.finance.settlements import (
    inspect_settlement,
    list_participant_settlements,
    preview_settlement,
)
from core_console.modules.finance.submission_api import _header_parameter, _post_create
from core_console.modules.finance.submission_schemas import (
    SettlementCreatedReceipt,
    SettlementFailureResponse,
)
from core_console.modules.users.dependencies import require_active_user
from core_console.modules.users.identity import CurrentUser
from core_console.problems import ProblemDetails

router = APIRouter(prefix="/api/finance/ledgers/{ledgerId}", tags=["Finance"])

_READ_ERRORS: dict[int | str, dict[str, Any]] = {
    status: {"model": ProblemDetails} for status in (403, 404, 422, 500, 503)
}


def _request_schema(model: type[SettlementDraftV1]) -> dict[str, object]:
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def inline(value: Any) -> Any:
        if isinstance(value, dict):
            if "$ref" in value:
                return inline(definitions[value["$ref"].rsplit("/", 1)[1]])
            return {key: inline(item) for key, item in value.items()}
        if isinstance(value, list):
            return [inline(item) for item in value]
        return value

    return cast(dict[str, object], inline(schema))


@router.post(
    "/settlements/preview",
    operation_id="previewFinanceSettlement",
    response_model=SettlementPreview,
    responses={**_READ_ERRORS, 409: {"model": ProblemDetails}},
    openapi_extra={
        "security": [],
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": _request_schema(SettlementDraftV1)}},
        },
    },
)
async def post_preview(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    request: Request,
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SettlementPreview:
    await _run_finance_workflow(require_advance_ledger(session, actor.id, ledger_id))
    try:
        draft = SettlementDraftV1.model_validate_json(await request.body())
    except ValidationError:
        raise _problem(
            422, "validation_error", "The request did not satisfy the API contract."
        ) from None
    return await _run_finance_workflow(
        preview_settlement(session, owner_id=actor.id, ledger_id=ledger_id, draft=draft)
    )


@router.post(
    "/settlements",
    description=(
        "Confirm one participant's accumulated cash repayment. Version 1 requires the "
        "matching previewToken and allocates all eligible debt oldest Advance date first. "
        "No selected subset, forgiveness, surplus or credit disposition is accepted. "
        "Prepare one immutable submission identity before dispatch; recover under that "
        "same identity using getFinanceSubmission or an explicit retry."
    ),
    operation_id="createFinanceSettlement",
    status_code=201,
    response_model=SettlementCreatedReceipt,
    responses={
        **_READ_ERRORS,
        400: {"model": ProblemDetails},
        **{status: {"model": SettlementFailureResponse} for status in (404, 409, 422)},
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
            "content": {"application/json": {"schema": _request_schema(SettlementCommandV1)}},
        },
    },
)
async def post_settlement(
    request: Request,
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    return await _post_create(request, actor, session, "createFinanceSettlement", ledger_id)


@router.get(
    "/settlements/{settlementId}",
    operation_id="getFinanceSettlement",
    response_model=SettlementResponse,
    responses=_READ_ERRORS,
    openapi_extra={"security": []},
)
async def get_settlement(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    settlement_id: Annotated[UUID, Path(alias="settlementId")],
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SettlementResponse:
    return await _run_finance_workflow(
        inspect_settlement(
            session, owner_id=actor.id, ledger_id=ledger_id, settlement_id=settlement_id
        )
    )


@router.get(
    "/participants/{participantId}/settlements",
    operation_id="listFinanceParticipantSettlements",
    response_model=list[SettlementResponse],
    responses=_READ_ERRORS,
    openapi_extra={"security": []},
)
async def get_settlements(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    participant_id: Annotated[UUID, Path(alias="participantId")],
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[SettlementResponse]:
    return await _run_finance_workflow(
        list_participant_settlements(
            session, owner_id=actor.id, ledger_id=ledger_id, participant_id=participant_id
        )
    )
