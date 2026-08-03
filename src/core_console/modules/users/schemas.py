"""HTTP schemas for the users module."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MeResponse(BaseModel):
    """Public fields returned for the active current user."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: UUID
    username: str | None
    display_name: str | None = Field(alias="displayName")
    email: str | None
