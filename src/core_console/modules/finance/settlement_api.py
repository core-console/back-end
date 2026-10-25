"""Backend producer HTTP boundary for versioned Settlement workflows."""

from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.database.dependencies import get_session
from core_console.modules.finance.advances import _problem, require_advance_ledger
from core_console.modules.finance.api import _run_finance_workflow
from core_console.modules.finance.settlement_schemas import (
    CombinedSettlementCommandV6,
    CombinedSettlementDraftV6,
    CombinedSettlementPreview,
    CreditOriginResponse,
    CreditSettlementPreview,
    ForgivenessCommandV5,
    ForgivenessDraftV5,
    ForgivenessPreview,
    IncomeSettlementPreview,
    ParticipantPositionResponse,
    SettlementCommandV1,
    SettlementCommandV2,
    SettlementCommandV3,
    SettlementCommandV4,
    SettlementDraftV1,
    SettlementDraftV2,
    SettlementDraftV3,
    SettlementDraftV4,
    SettlementPreview,
    SettlementSource,
)
from core_console.modules.finance.settlements import (
    inspect_settlement,
    list_participant_credits,
    list_participant_settlements,
    participant_positions,
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


def _request_schema(model: Any) -> dict[str, object]:
    schema = TypeAdapter(model).json_schema()
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
    response_model=CombinedSettlementPreview
    | ForgivenessPreview
    | IncomeSettlementPreview
    | CreditSettlementPreview
    | SettlementPreview,
    responses={**_READ_ERRORS, 409: {"model": ProblemDetails}},
    openapi_extra={
        "security": [],
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": _request_schema(
                        SettlementDraftV1
                        | SettlementDraftV2
                        | SettlementDraftV3
                        | SettlementDraftV4
                        | CombinedSettlementDraftV6
                        | ForgivenessDraftV5
                    )
                }
            },
        },
    },
)
async def post_preview(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    request: Request,
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> (
    SettlementPreview
    | CreditSettlementPreview
    | IncomeSettlementPreview
    | ForgivenessPreview
    | CombinedSettlementPreview
):
    await _run_finance_workflow(require_advance_ledger(session, actor.id, ledger_id))
    try:
        draft: (
            SettlementDraftV1
            | SettlementDraftV2
            | SettlementDraftV3
            | SettlementDraftV4
            | CombinedSettlementDraftV6
            | ForgivenessDraftV5
        ) = TypeAdapter(
            SettlementDraftV1
            | SettlementDraftV2
            | SettlementDraftV3
            | SettlementDraftV4
            | CombinedSettlementDraftV6
            | ForgivenessDraftV5
        ).validate_json(await request.body())
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
        "Version 2 requires excessDisposition=retainCredit and records any unallocated "
        "actual receipt as a participant credit Liability, including without eligible debt. "
        "Version 3 requires excessDisposition=acceptIncome and one complete Category Allocation "
        "equal to the positive actual surplus, creating linked Income without credit. "
        "Version 4 requires selectedObligationIds and allocates only within that eligible "
        "outstanding subset, with the same explicit excess choices. "
        "Version 5 requires kind=forgiveness, selectedObligationIds, a positive amount and reason. "
        "It records non-cash Expense against selected receivables without a receiving Account. "
        "Version 6 requires kind=combined, selectedObligationIds, positive cash amount, "
        "positive same-currency forgivenessAmount and reason. Cash allocates first, then "
        "forgiveness within remaining selected capacity; one atomic outcome retains both. "
        "Credit use and Corrections are not supported. "
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
            "content": {
                "application/json": {
                    "schema": _request_schema(
                        SettlementCommandV1
                        | SettlementCommandV2
                        | SettlementCommandV3
                        | SettlementCommandV4
                        | ForgivenessCommandV5
                        | CombinedSettlementCommandV6
                    )
                }
            },
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
    response_model=SettlementSource,
    responses=_READ_ERRORS,
    openapi_extra={"security": []},
)
async def get_settlement(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    settlement_id: Annotated[UUID, Path(alias="settlementId")],
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SettlementSource:
    return await _run_finance_workflow(
        inspect_settlement(
            session, owner_id=actor.id, ledger_id=ledger_id, settlement_id=settlement_id
        )
    )


@router.get(
    "/participants/{participantId}/settlements",
    operation_id="listFinanceParticipantSettlements",
    response_model=list[SettlementSource],
    responses=_READ_ERRORS,
    openapi_extra={"security": []},
)
async def get_settlements(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    participant_id: Annotated[UUID, Path(alias="participantId")],
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[SettlementSource]:
    return await _run_finance_workflow(
        list_participant_settlements(
            session, owner_id=actor.id, ledger_id=ledger_id, participant_id=participant_id
        )
    )


@router.get(
    "/participants/{participantId}/credits",
    operation_id="listFinanceParticipantCredits",
    description="Receipt-backed credit origins in immutable receipt confirmation order. "
    "Financial event dates do not change origin ordering; credit use and return are not supported.",
    response_model=list[CreditOriginResponse],
    responses=_READ_ERRORS,
    openapi_extra={"security": []},
)
async def get_credits(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    participant_id: Annotated[UUID, Path(alias="participantId")],
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[CreditOriginResponse]:
    return await _run_finance_workflow(
        list_participant_credits(
            session, owner_id=actor.id, ledger_id=ledger_id, participant_id=participant_id
        )
    )


@router.get(
    "/participants/{participantId}/positions",
    operation_id="getFinanceParticipantPositions",
    description="Gross outstanding receivables and available credits separately by currency, "
    "with net exposure as an additional summary. Creating an Advance never consumes credit.",
    response_model=list[ParticipantPositionResponse],
    responses=_READ_ERRORS,
    openapi_extra={"security": []},
)
async def get_positions(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    participant_id: Annotated[UUID, Path(alias="participantId")],
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[ParticipantPositionResponse]:
    return await _run_finance_workflow(
        participant_positions(
            session, owner_id=actor.id, ledger_id=ledger_id, participant_id=participant_id
        )
    )
