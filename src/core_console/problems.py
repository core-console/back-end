"""RFC 9457-style Problem Details models and FastAPI handlers."""

import logging
from http import HTTPStatus
from typing import Annotated

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

PROBLEM_MEDIA_TYPE = "application/problem+json"
logger = logging.getLogger(__name__)


class ProblemDetails(BaseModel):
    """Machine-readable API error response."""

    model_config = ConfigDict(extra="allow")

    type: Annotated[
        str,
        Field(
            json_schema_extra={"format": "uri-reference"},
        ),
    ]
    title: Annotated[str, Field(min_length=1)]
    status: Annotated[int, Field(ge=100, le=599)]
    detail: str | None = None
    instance: Annotated[
        str | None,
        Field(
            default=None,
            json_schema_extra={"format": "uri-reference"},
        ),
    ]
    code: str | None = Field(
        default=None,
        description="Stable machine-readable application error code.",
    )


class ApplicationProblem(Exception):
    """An expected application failure that is safe to expose."""

    def __init__(
        self,
        *,
        status: int,
        title: str,
        detail: str,
        code: str,
        type_uri: str = "about:blank",
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.title = title
        self.detail = detail
        self.code = code
        self.type_uri = type_uri


def problem_response(problem: ProblemDetails) -> JSONResponse:
    """Serialize a Problem Details response with its registered media type."""

    return JSONResponse(
        status_code=problem.status,
        content=problem.model_dump(mode="json", exclude_none=True),
        media_type=PROBLEM_MEDIA_TYPE,
    )


def _status_title(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "HTTP Error"


def install_problem_handlers(app: FastAPI) -> None:
    """Install consistent error translation at the HTTP boundary."""

    @app.exception_handler(ApplicationProblem)
    async def handle_application_problem(
        request: Request,
        exc: ApplicationProblem,
    ) -> Response:
        return problem_response(
            ProblemDetails(
                type=exc.type_uri,
                title=exc.title,
                status=exc.status,
                detail=exc.detail,
                instance=request.url.path,
                code=exc.code,
            )
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request,
        exc: StarletteHTTPException,
    ) -> Response:
        detail = exc.detail if isinstance(exc.detail, str) else _status_title(exc.status_code)
        code = "not_found" if exc.status_code == HTTPStatus.NOT_FOUND else "http_error"
        return problem_response(
            ProblemDetails(
                type="about:blank",
                title=_status_title(exc.status_code),
                status=exc.status_code,
                detail=detail,
                instance=request.url.path,
                code=code,
            )
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_exception(
        request: Request,
        exc: RequestValidationError,
    ) -> Response:
        problem = ProblemDetails.model_validate(
            {
                "type": "about:blank",
                "title": "Unprocessable Entity",
                "status": HTTPStatus.UNPROCESSABLE_ENTITY,
                "detail": "The request did not satisfy the API contract.",
                "instance": request.url.path,
                "code": "validation_error",
                "errors": exc.errors(),
            }
        )
        return problem_response(problem)

    @app.exception_handler(Exception)
    async def handle_unexpected_exception(request: Request, exc: Exception) -> Response:
        logger.exception(
            "Unhandled exception while serving %s %s",
            request.method,
            request.url.path,
            exc_info=exc,
        )
        return problem_response(
            ProblemDetails(
                type="about:blank",
                title="Internal Server Error",
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
                detail="An unexpected error occurred.",
                instance=request.url.path,
                code="internal_error",
            )
        )
