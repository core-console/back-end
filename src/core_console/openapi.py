"""Deterministic frontend business API contract generation."""

from typing import Any, cast

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from core_console import __version__
from core_console.problems import PROBLEM_MEDIA_TYPE

API_PREFIX = "/api"
_PROBLEM_DETAILS_REF = "#/components/schemas/ProblemDetails"
_HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

OPENAPI_TAGS = [
    {
        "name": "Hello",
        "description": "Initial API connectivity endpoints",
    },
    {
        "name": "Users",
        "description": "Current-user endpoints",
    },
    {
        "name": "Finance",
        "description": "Personal Finance endpoints",
    },
]


def _use_problem_details_media_type(contract_paths: dict[str, Any]) -> None:
    """Normalize every Problem Details response to its registered media type."""

    for path_item in contract_paths.values():
        if not isinstance(path_item, dict):
            continue
        for method in _HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            responses = operation.get("responses")
            if not isinstance(responses, dict):
                continue
            for response in responses.values():
                if not isinstance(response, dict):
                    continue
                content = response.get("content")
                if not isinstance(content, dict):
                    continue
                json_content = content.get("application/json")
                if not isinstance(json_content, dict):
                    continue
                if json_content.get("schema") != {"$ref": _PROBLEM_DETAILS_REF}:
                    continue
                content[PROBLEM_MEDIA_TYPE] = content.pop("application/json")


class CoreConsoleApp(FastAPI):
    """FastAPI application whose schema preserves the frontend server prefix."""

    def openapi(self) -> dict[str, Any]:
        if self.openapi_schema is not None:
            return self.openapi_schema

        schema = get_openapi(
            title="Core Console API",
            version=__version__,
            description=(
                "Initial backend-owned API definition for Core Console. "
                "Operational endpoints are intentionally excluded."
            ),
            openapi_version="3.1.1",
            routes=self.routes,
            tags=OPENAPI_TAGS,
        )

        paths = cast(dict[str, Any], schema["paths"])
        contract_paths: dict[str, Any] = {}
        for path, operations in paths.items():
            if not path.startswith(f"{API_PREFIX}/"):
                continue
            contract_paths[path.removeprefix(API_PREFIX)] = operations
        schema["paths"] = contract_paths
        schema["jsonSchemaDialect"] = "https://json-schema.org/draft/2020-12/schema"
        schema["servers"] = [
            {
                "url": API_PREFIX,
                "description": "Same-origin API gateway",
            }
        ]
        _use_problem_details_media_type(contract_paths)

        self.openapi_schema = schema
        return schema
