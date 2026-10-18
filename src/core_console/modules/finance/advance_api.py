"""Backend producer HTTP boundary for versioned Advance workflows."""

from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.database.dependencies import get_session
from core_console.modules.finance.advance_schemas import (
    AdvanceCommandV1,
    AdvanceCommandV2,
    AdvanceDraftV1,
    AdvanceDraftV2,
    AdvancePreview,
    AdvanceResponse,
    ObligationResponse,
    SharedAdvancePreview,
    SharedAdvanceResponse,
)
from core_console.modules.finance.advances import (
    inspect_advance,
    list_participant_obligations,
    preview_advance,
)
from core_console.modules.finance.api import _run_finance_workflow
from core_console.modules.finance.submission_api import _header_parameter, _post_create
from core_console.modules.finance.submission_schemas import (
    AdvanceCreatedReceipt,
    AdvanceFailureResponse,
)
from core_console.modules.users.dependencies import require_active_user
from core_console.modules.users.identity import CurrentUser
from core_console.problems import ProblemDetails

router = APIRouter(prefix="/api/finance/ledgers/{ledgerId}", tags=["Finance"])

_READ_ERRORS: dict[int | str, dict[str, Any]] = {
    status: {"model": ProblemDetails} for status in (403, 404, 422, 500, 503)
}


def _command_schema() -> dict[str, object]:
    schema = TypeAdapter(AdvanceCommandV1 | AdvanceCommandV2).json_schema()
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
    "/advances/preview",
    operation_id="previewFinanceAdvance",
    response_model=SharedAdvancePreview | AdvancePreview,
    responses={**_READ_ERRORS, 409: {"model": ProblemDetails}},
    openapi_extra={"security": []},
)
async def post_preview(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    draft: AdvanceDraftV1 | AdvanceDraftV2,
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AdvancePreview | SharedAdvancePreview:
    return await _run_finance_workflow(
        preview_advance(session, owner_id=actor.id, ledger_id=ledger_id, draft=draft)
    )


@router.post(
    "/advances",
    description=(
        "Confirm one atomic Advance under the retained submission protocol. "
        "Finance-Command-Version: 1 accepts the frozen single-participant, Asset-funded "
        "pure Advance body. Version 2 accepts participantShares, an explicit non-negative "
        "ownerShare and Asset or Liability funding. Each body's previewToken must come "
        "from its matching preview. All linked effects share one terminal outcome."
    ),
    operation_id="createFinanceAdvance",
    status_code=201,
    response_model=AdvanceCreatedReceipt,
    responses={
        **_READ_ERRORS,
        400: {"model": ProblemDetails},
        **{status: {"model": AdvanceFailureResponse} for status in (404, 409, 422)},
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
            "content": {"application/json": {"schema": _command_schema()}},
        },
    },
)
async def post_advance(
    request: Request,
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    return await _post_create(request, actor, session, "createFinanceAdvance", ledger_id)


@router.get(
    "/advances/{advanceId}",
    operation_id="getFinanceAdvance",
    response_model=AdvanceResponse | SharedAdvanceResponse,
    responses=_READ_ERRORS,
    openapi_extra={"security": []},
)
async def get_advance(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    advance_id: Annotated[UUID, Path(alias="advanceId")],
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AdvanceResponse | SharedAdvanceResponse:
    return await _run_finance_workflow(
        inspect_advance(session, owner_id=actor.id, ledger_id=ledger_id, advance_id=advance_id)
    )


@router.get(
    "/participants/{participantId}/obligations",
    operation_id="listFinanceParticipantObligations",
    response_model=list[ObligationResponse],
    responses=_READ_ERRORS,
    openapi_extra={"security": []},
)
async def get_obligations(
    ledger_id: Annotated[UUID, Path(alias="ledgerId")],
    participant_id: Annotated[UUID, Path(alias="participantId")],
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[ObligationResponse]:
    return await _run_finance_workflow(
        list_participant_obligations(
            session, owner_id=actor.id, ledger_id=ledger_id, participant_id=participant_id
        )
    )
