"""Current-user HTTP routes."""

from typing import Annotated

from fastapi import APIRouter, Depends

from core_console.modules.users.dependencies import require_active_user
from core_console.modules.users.identity import CurrentUser
from core_console.modules.users.schemas import MeResponse
from core_console.problems import ProblemDetails

router = APIRouter(prefix="/api", tags=["Users"])


@router.get(
    "/me",
    operation_id="getCurrentUser",
    summary="Get the current user",
    description="Returns the public profile of the active provisioned user.",
    response_model=MeResponse,
    responses={
        403: {
            "model": ProblemDetails,
            "description": "Access is denied.",
        },
        503: {
            "model": ProblemDetails,
            "description": "PostgreSQL is not available.",
        },
        500: {
            "model": ProblemDetails,
            "description": "An unexpected server error occurred.",
        },
    },
    openapi_extra={"security": []},
)
async def get_me(
    user: Annotated[CurrentUser, Depends(require_active_user)],
) -> MeResponse:
    """Return the active current user's public profile."""

    return MeResponse(
        id=user.id,
        username=user.username,
        displayName=user.display_name,
        email=user.email,
    )
