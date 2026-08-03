"""Identity boundary objects for the users module."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ExternalIdentity:
    """Issuer-scoped subject supplied by the configured identity source."""

    issuer: str
    subject: str


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """Request-safe projection of a persisted local user."""

    id: UUID
    username: str | None
    display_name: str | None
    email: str | None
    status: Literal["active", "disabled"]
