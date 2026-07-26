"""Hello module HTTP routes."""

from fastapi import APIRouter

from core_console.modules.hello.schemas import HelloWorldResponse
from core_console.problems import PROBLEM_MEDIA_TYPE, ProblemDetails

router = APIRouter(prefix="/api", tags=["Hello"])


@router.get(
    "/helloWorld",
    operation_id="getHelloWorld",
    summary="Get the hello world message",
    description="Returns a minimal response used to verify API connectivity.",
    response_model=HelloWorldResponse,
    responses={
        200: {
            "description": "The hello world message was returned successfully.",
            "content": {
                "application/json": {
                    "example": {
                        "message": "Hello, world!",
                    }
                }
            },
        },
        500: {
            "model": ProblemDetails,
            "description": "An unexpected server error occurred.",
            "content": {
                PROBLEM_MEDIA_TYPE: {
                    "example": {
                        "type": "about:blank",
                        "title": "Internal Server Error",
                        "status": 500,
                        "detail": "An unexpected error occurred.",
                        "code": "internal_error",
                    }
                }
            },
        },
    },
    openapi_extra={"security": []},
)
async def get_hello_world() -> HelloWorldResponse:
    """Return the response defined by the existing frontend contract."""

    return HelloWorldResponse(message="Hello, world!")
