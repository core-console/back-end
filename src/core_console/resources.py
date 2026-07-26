"""Application-owned resources and state access."""

from dataclasses import dataclass
from typing import cast

from fastapi import FastAPI

from core_console.config import Settings
from core_console.database.resources import DatabaseResources


@dataclass(frozen=True, slots=True)
class ApplicationResources:
    """Resources created and disposed by the application lifespan."""

    settings: Settings
    database: DatabaseResources | None


def get_application_resources(app: FastAPI) -> ApplicationResources:
    """Read typed resources after lifespan startup."""

    return cast(ApplicationResources, app.state.resources)
