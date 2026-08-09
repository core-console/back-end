"""HTTP schemas for the users module."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MeResponse(BaseModel):
    """Public fields returned for the active current user."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: UUID
    username: str | None
    display_name: str | None = Field(alias="displayName")
    email: str | None


class UserResponse(BaseModel):
    """Stable management projection of one persisted local user."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: UUID
    display_name: str | None = Field(alias="displayName")
    username: str | None
    email: str | None
    identity_issuer: str = Field(alias="identityIssuer")
    identity_subject: str = Field(alias="identitySubject")
    status: Literal["active", "inactive"]


class _RequestModel(BaseModel):
    """Accept only the JSON aliases declared by the OpenAPI contract."""

    model_config = ConfigDict(
        extra="forbid",
        validate_by_alias=True,
        validate_by_name=False,
    )


class CreateUserRequest(_RequestModel):
    """Administrator-supplied profile and immutable external identity."""

    display_name: str | None = Field(alias="displayName")
    username: str | None
    email: str | None
    identity_issuer: str = Field(alias="identityIssuer")
    identity_subject: str = Field(alias="identitySubject")


class UpdateUserRequest(_RequestModel):
    """Partial profile changes; field presence carries PATCH semantics."""

    display_name: str | None = Field(default=None, alias="displayName")
    username: str | None = None
    email: str | None = None
