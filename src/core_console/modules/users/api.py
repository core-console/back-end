"""Current-user and Users v1 management HTTP routes."""

from collections.abc import Awaitable
from http import HTTPStatus
from typing import Annotated, Literal, Never
from uuid import UUID

from fastapi import APIRouter, Depends, Path
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from core_console.database.dependencies import get_session
from core_console.modules.users.dependencies import is_database_unavailable, require_active_user
from core_console.modules.users.identity import CurrentUser
from core_console.modules.users.models import User
from core_console.modules.users.queries import list_users
from core_console.modules.users.schemas import (
    CreateUserRequest,
    MeResponse,
    UpdateUserRequest,
    UserResponse,
)
from core_console.modules.users.service import (
    CannotDeactivateSelfError,
    InvalidUserRequestError,
    UserConflictError,
    UserNotFoundError,
    UserProfileUpdate,
    create_managed_user,
    deactivate_managed_user,
    reactivate_managed_user,
    update_managed_user,
)
from core_console.problems import ApplicationProblem, ProblemDetails

router = APIRouter(prefix="/api", tags=["Users"])

_PUBLIC_STATUS_BY_INTERNAL: dict[str, Literal["active", "inactive"]] = {
    "active": "active",
    "disabled": "inactive",
}


def _to_user_response(user: User) -> UserResponse:
    """Map internal persistence values to the management API contract."""

    return UserResponse(
        id=user.id,
        displayName=user.display_name,
        username=user.username,
        email=user.email,
        identityIssuer=user.identity_issuer,
        identitySubject=user.identity_subject,
        status=_PUBLIC_STATUS_BY_INTERNAL[user.status],
    )


async def _run_management_workflow[Result](workflow: Awaitable[Result]) -> Result:
    """Translate database availability failures at the management HTTP boundary."""

    try:
        return await workflow
    except (InterfaceError, OperationalError) as exc:
        if not is_database_unavailable(exc):
            raise
        raise ApplicationProblem(
            status=HTTPStatus.SERVICE_UNAVAILABLE,
            title="Service Unavailable",
            detail="PostgreSQL is not available.",
            code="database_unavailable",
        ) from None


def _raise_user_not_found() -> Never:
    """Raise the stable management problem for a missing target user."""

    raise ApplicationProblem(
        status=HTTPStatus.NOT_FOUND,
        title="Not Found",
        detail="The requested user does not exist.",
        code="user_not_found",
    ) from None


def _raise_invalid_user_request(error: InvalidUserRequestError) -> Never:
    """Raise the existing validation problem for a Users v1 business rule."""

    raise ApplicationProblem(
        status=HTTPStatus.UNPROCESSABLE_ENTITY,
        title="Unprocessable Entity",
        detail=str(error),
        code="validation_error",
    ) from None


@router.get(
    "/users",
    operation_id="listUsers",
    summary="List users",
    description="Returns every local user in stable creation order.",
    response_model=list[UserResponse],
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
async def get_users(
    _: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[UserResponse]:
    """Return all local users for profile management."""

    users = await _run_management_workflow(list_users(session))
    return [_to_user_response(user) for user in users]


@router.post(
    "/users",
    operation_id="createUser",
    summary="Create a user",
    description="Creates an active local user with an immutable external identity mapping.",
    status_code=201,
    response_model=UserResponse,
    responses={
        403: {
            "model": ProblemDetails,
            "description": "Access is denied.",
        },
        409: {
            "model": ProblemDetails,
            "description": "The external identity is already mapped.",
        },
        422: {
            "model": ProblemDetails,
            "description": "The request does not satisfy the user contract.",
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
async def post_user(
    request: CreateUserRequest,
    _: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserResponse:
    """Create one active local user."""

    try:
        user = await _run_management_workflow(
            create_managed_user(
                session,
                display_name=request.display_name,
                username=request.username,
                email=request.email,
                identity_issuer=request.identity_issuer,
                identity_subject=request.identity_subject,
            )
        )
    except InvalidUserRequestError as exc:
        _raise_invalid_user_request(exc)
    except UserConflictError:
        raise ApplicationProblem(
            status=HTTPStatus.CONFLICT,
            title="Conflict",
            detail="The external identity is already mapped to a local user.",
            code="user_conflict",
        ) from None
    return _to_user_response(user)


@router.patch(
    "/users/{userId}",
    operation_id="updateUser",
    summary="Update a user profile",
    description="Partially updates mutable profile fields for one local user.",
    response_model=UserResponse,
    responses={
        403: {
            "model": ProblemDetails,
            "description": "Access is denied.",
        },
        404: {
            "model": ProblemDetails,
            "description": "The user does not exist.",
        },
        422: {
            "model": ProblemDetails,
            "description": "The request does not satisfy the user contract.",
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
async def patch_user(
    user_id: Annotated[UUID, Path(alias="userId")],
    request: UpdateUserRequest,
    _: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserResponse:
    """Update one local user's mutable profile."""

    updates: UserProfileUpdate = {}
    if "display_name" in request.model_fields_set:
        updates["display_name"] = request.display_name
    if "username" in request.model_fields_set:
        updates["username"] = request.username
    if "email" in request.model_fields_set:
        updates["email"] = request.email

    try:
        user = await _run_management_workflow(
            update_managed_user(session, user_id=user_id, updates=updates)
        )
    except UserNotFoundError:
        _raise_user_not_found()
    except InvalidUserRequestError as exc:
        _raise_invalid_user_request(exc)
    return _to_user_response(user)


@router.post(
    "/users/{userId}/deactivate",
    operation_id="deactivateUser",
    summary="Deactivate a user",
    description="Disables one local user without deleting it or changing its identity mapping.",
    response_model=UserResponse,
    responses={
        403: {
            "model": ProblemDetails,
            "description": "Access is denied.",
        },
        404: {
            "model": ProblemDetails,
            "description": "The user does not exist.",
        },
        409: {
            "model": ProblemDetails,
            "description": "The current user cannot deactivate themselves.",
        },
        422: {
            "model": ProblemDetails,
            "description": "The user identifier is invalid.",
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
async def post_deactivate_user(
    user_id: Annotated[UUID, Path(alias="userId")],
    actor: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserResponse:
    """Deactivate one local user other than the current actor."""

    try:
        user = await _run_management_workflow(
            deactivate_managed_user(
                session,
                user_id=user_id,
                actor_id=actor.id,
            )
        )
    except UserNotFoundError:
        _raise_user_not_found()
    except CannotDeactivateSelfError:
        raise ApplicationProblem(
            status=HTTPStatus.CONFLICT,
            title="Conflict",
            detail="The current user cannot be deactivated.",
            code="cannot_deactivate_self",
        ) from None
    return _to_user_response(user)


@router.post(
    "/users/{userId}/reactivate",
    operation_id="reactivateUser",
    summary="Reactivate a user",
    description="Returns one disabled local user to active service.",
    response_model=UserResponse,
    responses={
        403: {
            "model": ProblemDetails,
            "description": "Access is denied.",
        },
        404: {
            "model": ProblemDetails,
            "description": "The user does not exist.",
        },
        422: {
            "model": ProblemDetails,
            "description": "The user identifier is invalid.",
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
async def post_reactivate_user(
    user_id: Annotated[UUID, Path(alias="userId")],
    _: Annotated[CurrentUser, Depends(require_active_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserResponse:
    """Reactivate one local user."""

    try:
        user = await _run_management_workflow(reactivate_managed_user(session, user_id=user_id))
    except UserNotFoundError:
        _raise_user_not_found()
    return _to_user_response(user)


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
