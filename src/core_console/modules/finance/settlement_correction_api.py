"""Authoritative preview, durable confirmation and immutable correction history."""

from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.database.dependencies import get_session
from core_console.modules.finance.advances import _problem, require_advance_ledger
from core_console.modules.finance.api import _run_finance_workflow
from core_console.modules.finance.settlement_correction_schemas import (
    SettlementCorrectionCommandV1,
    SettlementCorrectionDraftV1,
    SettlementCorrectionPreview,
    SettlementCorrectionResponse,
)
from core_console.modules.finance.settlement_corrections import (
    inspect_settlement_correction,
    list_settlement_corrections,
    preview_settlement_correction,
)
from core_console.modules.finance.submission_api import _header_parameter, _post_create
from core_console.modules.finance.submission_schemas import (
    SettlementCorrectionCreatedReceipt,
    SettlementCorrectionFailureResponse,
)
from core_console.modules.users.dependencies import require_active_user
from core_console.modules.users.identity import CurrentUser
from core_console.problems import ProblemDetails

router = APIRouter(prefix="/api/finance/ledgers/{ledgerId}", tags=["Finance"])
_ERRORS: dict[int | str, dict[str, Any]] = {
    s: {"model": ProblemDetails} for s in (403, 404, 409, 422, 500, 503)
}


def _request_schema(model: type[SettlementCorrectionDraftV1]) -> dict[str, object]:
    schema = model.model_json_schema()
    definitions = schema.pop("$defs")

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
    "/settlement-corrections/preview",
    operation_id="previewFinanceSettlementCorrection",
    response_model=SettlementCorrectionPreview,
    responses=_ERRORS,
    openapi_extra={
        "security": [],
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": _request_schema(SettlementCorrectionDraftV1)}
            },
        },
    },
)
async def post_preview(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    request: Request,
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SettlementCorrectionPreview:
    await _run_finance_workflow(require_advance_ledger(session, actor.id, ledger_id))
    try:
        draft = SettlementCorrectionDraftV1.model_validate_json(await request.body())
    except ValidationError:
        raise _problem(
            422, "validation_error", "The request did not satisfy the API contract."
        ) from None
    return await _run_finance_workflow(
        preview_settlement_correction(session, owner_id=actor.id, ledger_id=ledger_id, draft=draft)
    )


@router.post(
    "/settlement-corrections",
    operation_id="createFinanceSettlementCorrection",
    status_code=201,
    response_model=SettlementCorrectionCreatedReceipt,
    responses={
        **_ERRORS,
        400: {"model": ProblemDetails},
        **{s: {"model": SettlementCorrectionFailureResponse} for s in (404, 409, 422)},
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
            "content": {
                "application/json": {"schema": _request_schema(SettlementCorrectionCommandV1)}
            },
        },
    },
)
async def post_correction(
    request: Request,
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    return await _post_create(
        request, actor, session, "createFinanceSettlementCorrection", ledger_id
    )


@router.get(
    "/settlement-corrections/{correctionId}",
    operation_id="getFinanceSettlementCorrection",
    response_model=SettlementCorrectionResponse,
    responses=_ERRORS,
    openapi_extra={"security": []},
)
async def get_correction(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    correction_id: Annotated[UUID, Path(alias="correctionId")],
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SettlementCorrectionResponse:
    return await _run_finance_workflow(
        inspect_settlement_correction(
            session, owner_id=actor.id, ledger_id=ledger_id, correction_id=correction_id
        )
    )


@router.get(
    "/settlements/{settlementId}/corrections",
    operation_id="listFinanceSettlementCorrections",
    response_model=list[SettlementCorrectionResponse],
    responses=_ERRORS,
    openapi_extra={"security": []},
)
async def get_history(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    settlement_id: Annotated[UUID, Path(alias="settlementId")],
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[SettlementCorrectionResponse]:
    return await _run_finance_workflow(
        list_settlement_corrections(
            session, owner_id=actor.id, ledger_id=ledger_id, settlement_id=settlement_id
        )
    )
